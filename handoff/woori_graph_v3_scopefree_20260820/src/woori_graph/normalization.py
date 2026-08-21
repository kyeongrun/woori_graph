"""Conservative first-pass normalization for the human-review dictionaries."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .extraction import CompletionClient
from .entity_resolution import resolve_entity_name
from .ids import stable_id
from .prompting import load_prompt_asset


_NEGATIVE_CUE_RE = re.compile(r"(?:않|아니|못|없(?:다|는|어|었|을|고|으))")


RELATION_NORMALIZATION_PROMPT = """다음은 법령에서 추출된 원시 술어 목록이다.
각 표현을 세밀한 법률 의미가 아니라 넓은 대표 행위 타입으로 정규화하라.
약간의 의미 차이, 수동·능동 차이, 연결어미, 뒤따르는 부수 행위의 생략을 허용한다.
단, 긍정과 부정은 반드시 별도 타입으로 둔다.

규칙:
- 긍정 canonical_name은 원칙적으로 `<핵심 행위>하다` 형식이다.
- 부정·금지·불가 canonical_name은 `<핵심 행위>하지않다` 형식이다.
- 의무, 허용, 가능, 명령, 수동·능동, 시제 차이는 타입을 나누지 않는다.
- 복합 술어는 앞쪽 또는 문장 전체를 대표하는 핵심 행위 하나만 남긴다. 뒤의 제출·비치·교부·제공·운용 같은 부수 행위는 생략해도 된다.
- 조사, 목적지, 대상, 조건, 연결어미가 술어 문자열에 섞여 있으면 제거한다.
- 같은 입력 목록 안에서 같은 행위군에는 철자까지 동일한 canonical_name을 사용한다.
- `정한다`, `정하여야 한다`, `정할 수 있다`, `정해진다` → `정하다` / POSITIVE
- `정할 수 없다`, `정하지 아니한다` → `정하지않다` / NEGATIVE
- `정지된다`, `정지를 할 수 있다`, `정지한다` → `정지하다` / POSITIVE
- `정지할 수 없다`, `정지되지 아니한다` → `정지하지않다` / NEGATIVE
- `작성변경한다`, `작성ㆍ비치한다`, `작성비치하되`, `작성ㆍ운용`, `작성ㆍ제공할 수 있다`, `작성ㆍ제출하여야 한다`, `작성하여`, `작성하여 교부하여야 한다` → 모두 `작성하다` / POSITIVE
- `작성하지 않는다`, `작성ㆍ제공하지 아니한다` → `작성하지않다` / NEGATIVE
- `실시한다`, `수행한다`, `시행한다`처럼 문맥상 같은 실행 행위는 하나의 넓은 대표 타입으로 묶어도 된다.
- 원시 alias를 누락하거나 수정하지 않는다.
- JSON만 반환한다.

반환 형식:
{"items": [{"alias": "원시 술어", "canonical_name": "대표 관계", "polarity": "POSITIVE|NEGATIVE"}]}
"""

RELATION_NORMALIZATION_PROMPT = load_prompt_asset("relation_normalize.ko.md")


def propose_relation_mapping(
    raw_records: Sequence[dict[str, Any]],
    client: CompletionClient,
    *,
    batch_size: int = 80,
    workers: int = 4,
) -> tuple[dict[str, dict[str, str]], list[dict[str, Any]]]:
    examples = _relation_examples(raw_records)
    aliases = sorted(examples, key=lambda name: (-examples[name]["count"], name))
    mapping: dict[str, dict[str, str]] = {}
    errors: list[dict[str, Any]] = []
    batches = [(start, aliases[start : start + batch_size]) for start in range(0, len(aliases), batch_size)]

    def normalize_batch(start: int, batch_aliases: list[str]):
        payload = [
            {
                "alias": alias,
                "count": examples[alias]["count"],
                "samples": examples[alias]["samples"],
            }
            for alias in batch_aliases
        ]
        response = client.complete(
            f"{RELATION_NORMALIZATION_PROMPT}\n\n입력:\n{json.dumps(payload, ensure_ascii=False)}"
        )
        parsed = _parse_json_object(response)
        items = parsed.get("items")
        if not isinstance(items, list):
            raise ValueError("response must contain an items array")
        batch_mapping: dict[str, dict[str, str]] = {}
        for item in items:
            if not isinstance(item, Mapping):
                continue
            alias = item.get("alias")
            canonical_name = item.get("canonical_name")
            polarity = item.get("polarity")
            if (
                alias in batch_aliases
                and isinstance(canonical_name, str)
                and canonical_name.strip()
                and polarity in {"POSITIVE", "NEGATIVE"}
            ):
                canonical_name = canonical_name.strip()
                if _proposal_polarity_is_consistent(canonical_name, polarity):
                    batch_mapping[alias] = {
                        "canonical_name": canonical_name,
                        "polarity": polarity,
                        "normalization_status": "llm_proposed",
                    }
                else:
                    batch_mapping[alias] = {
                        "canonical_name": alias,
                        "polarity": _infer_raw_polarity(alias),
                        "normalization_status": "fallback_raw_invalid_proposal",
                    }
        return start, batch_aliases, batch_mapping

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(normalize_batch, start, batch_aliases): (start, batch_aliases)
            for start, batch_aliases in batches
        }
        for future in as_completed(futures):
            start, batch_aliases = futures[future]
            try:
                _, _, batch_mapping = future.result()
                mapping.update(batch_mapping)
            except Exception as exc:
                errors.append(
                    {
                        "batch_start": start,
                        "aliases": batch_aliases,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )

    for _, batch_aliases in batches:
        for alias in batch_aliases:
            mapping.setdefault(
                alias,
                {
                    "canonical_name": alias,
                    "polarity": _infer_raw_polarity(alias),
                    "normalization_status": "fallback_raw_missing_proposal",
                },
            )
    return mapping, errors


def _proposal_polarity_is_consistent(canonical_name: str, polarity: str) -> bool:
    has_negative_cue = bool(_NEGATIVE_CUE_RE.search(canonical_name))
    return has_negative_cue if polarity == "NEGATIVE" else not has_negative_cue


def _infer_raw_polarity(alias: str) -> str:
    return "NEGATIVE" if _NEGATIVE_CUE_RE.search(alias) else "POSITIVE"


def build_first_pass_normalization(
    raw_records: Sequence[dict[str, Any]],
    relation_mapping: Mapping[str, Mapping[str, str]],
    *,
    sample_limit: int = 5,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    entity_buckets: dict[str, dict[str, Any]] = {}
    relation_buckets: dict[tuple[str, str], dict[str, Any]] = {}
    edge_buckets: dict[tuple[str, str, str], dict[str, Any]] = {}

    for record in raw_records:
        for raw_relation in record.get("relations", []):
            source = _resolve_entity(
                raw_relation["subject"], record, entity_buckets, sample_limit
            )
            target = _resolve_entity(
                raw_relation["object"], record, entity_buckets, sample_limit
            )
            raw_predicate = raw_relation["predicate"]
            proposed = relation_mapping.get(raw_predicate, {})
            canonical_relation = proposed.get("canonical_name", raw_predicate)
            polarity = proposed.get("polarity", "POSITIVE")
            relation_type_id = stable_id("relation_type", canonical_relation)
            relation_key = (canonical_relation, polarity)
            relation_bucket = relation_buckets.setdefault(
                relation_key,
                {
                    "relation_type_id": relation_type_id,
                    "canonical_name": canonical_relation,
                    "polarity": polarity,
                    "aliases": {},
                    "mention_count": 0,
                },
            )
            relation_bucket["mention_count"] += 1
            alias_bucket = relation_bucket["aliases"].setdefault(
                raw_predicate,
                {"name": raw_predicate, "mention_count": 0, "sample_source_refs": []},
            )
            alias_bucket["mention_count"] += 1
            _add_sample(alias_bucket["sample_source_refs"], record, sample_limit)

            edge_key = (source["entity_id"], relation_type_id, target["entity_id"])
            edge = edge_buckets.setdefault(
                edge_key,
                {
                    "relation_id": stable_id("relation", *edge_key),
                    "source_entity_id": source["entity_id"],
                    "relation_type_id": relation_type_id,
                    "target_entity_id": target["entity_id"],
                    "source_name": source["canonical_name"],
                    "relation_name": canonical_relation,
                    "target_name": target["canonical_name"],
                    "evidence": [],
                },
            )
            edge["evidence"].append(
                {
                    "relation_mention_id": raw_relation["relation_mention_id"],
                    "semantic_unit_id": record["semantic_unit_id"],
                    "document_id": record["document_id"],
                    "source_ref": record["source_ref"],
                    "raw_subject": raw_relation["subject"],
                    "raw_predicate": raw_predicate,
                    "raw_object": raw_relation["object"],
                }
            )

    entities = []
    for bucket in entity_buckets.values():
        aliases = list(bucket.pop("_aliases").values())
        aliases.sort(key=lambda item: (-item["mention_count"], item["name"]))
        bucket["aliases"] = aliases
        entities.append(bucket)
    entities.sort(key=lambda item: (item["canonical_name"], item["entity_id"]))

    relations = []
    for bucket in relation_buckets.values():
        bucket["aliases"].setdefault(
            bucket["canonical_name"],
            {
                "name": bucket["canonical_name"],
                "mention_count": 0,
                "sample_source_refs": [],
            },
        )
        aliases = list(bucket["aliases"].values())
        for alias in aliases:
            alias["is_canonical"] = alias["name"] == bucket["canonical_name"]
        aliases.sort(
            key=lambda item: (not item["is_canonical"], -item["mention_count"], item["name"])
        )
        bucket["aliases"] = aliases
        relations.append(bucket)
    relations.sort(key=lambda item: item["canonical_name"])

    edges = list(edge_buckets.values())
    for edge in edges:
        edge["evidence_count"] = len(edge["evidence"])
    edges.sort(key=lambda item: item["relation_id"])
    return entities, relations, edges


def relation_mapping_records(mapping: Mapping[str, Mapping[str, str]]) -> list[dict[str, str]]:
    return [{"alias": alias, **dict(value)} for alias, value in sorted(mapping.items())]


def relation_mapping_from_records(
    records: Iterable[Mapping[str, Any]],
) -> dict[str, dict[str, str]]:
    """Load a reviewable relation map and revalidate polarity/name consistency."""
    mapping: dict[str, dict[str, str]] = {}
    for record in records:
        alias = record.get("alias")
        canonical_name = record.get("canonical_name")
        polarity = record.get("polarity")
        if not isinstance(alias, str) or not alias.strip():
            raise ValueError("relation-map record must contain a non-empty alias")
        if not isinstance(canonical_name, str) or not canonical_name.strip():
            raise ValueError(f"relation-map alias {alias!r} has no canonical_name")
        if polarity not in {"POSITIVE", "NEGATIVE"}:
            raise ValueError(f"relation-map alias {alias!r} has invalid polarity")
        alias = alias.strip()
        canonical_name = canonical_name.strip()
        if _proposal_polarity_is_consistent(canonical_name, polarity):
            mapping[alias] = {
                "canonical_name": canonical_name,
                "polarity": polarity,
                "normalization_status": str(
                    record.get("normalization_status", "review_map_input")
                ),
            }
        else:
            mapping[alias] = {
                "canonical_name": alias,
                "polarity": _infer_raw_polarity(alias),
                "normalization_status": "fallback_raw_invalid_proposal",
            }
    return mapping


def _resolve_entity(
    raw_name: str,
    record: dict[str, Any],
    buckets: dict[str, dict[str, Any]],
    sample_limit: int,
) -> dict[str, Any]:
    raw_name = raw_name.strip()
    canonical_name, method = resolve_entity_name(
        raw_name,
        document_title=record["document_title"],
    )

    entity_id = stable_id("entity", canonical_name)
    bucket = buckets.setdefault(
        canonical_name,
        {
            "entity_id": entity_id,
            "canonical_name": canonical_name,
            "_aliases": {},
            "sample_source_refs": [],
            "mention_count": 0,
            "resolution_methods": [],
        },
    )
    bucket["mention_count"] += 1
    if method not in bucket["resolution_methods"]:
        bucket["resolution_methods"].append(method)
    _add_sample(bucket["sample_source_refs"], record, sample_limit)
    # A self reference is contextual, not a globally reusable alias. Its raw
    # expression remains in edge evidence while the dictionary receives only
    # the resolved canonical document name.
    alias_name = canonical_name if method == "current_document_self_reference" else raw_name
    alias = bucket["_aliases"].setdefault(
        alias_name,
        {
            "name": alias_name,
            "resolution_method": method,
            "mention_count": 0,
            "sample_source_refs": [],
        },
    )
    alias["mention_count"] += 1
    alias["is_canonical"] = alias_name == canonical_name
    _add_sample(alias["sample_source_refs"], record, sample_limit)
    return bucket


def _relation_examples(raw_records: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    examples: dict[str, dict[str, Any]] = {}
    for record in raw_records:
        for relation in record.get("relations", []):
            predicate = relation["predicate"]
            bucket = examples.setdefault(predicate, {"count": 0, "samples": []})
            bucket["count"] += 1
            if len(bucket["samples"]) < 3:
                bucket["samples"].append(
                    {
                        "subject": relation["subject"],
                        "object": relation["object"],
                        "document_title": record["document_title"],
                    }
                )
    return examples


def _add_sample(samples: list[dict[str, Any]], record: dict[str, Any], limit: int) -> None:
    if len(samples) >= limit:
        return
    sample = {
        "semantic_unit_id": record["semantic_unit_id"],
        "document_id": record["document_id"],
        "source_ref": record["source_ref"],
    }
    if sample not in samples:
        samples.append(sample)


def _parse_json_object(response: str) -> dict[str, Any]:
    candidate = response.strip()
    if candidate.startswith("```"):
        candidate = candidate.split("\n", maxsplit=1)[-1]
        if candidate.rstrip().endswith("```"):
            candidate = candidate.rstrip()[:-3]
    value = json.loads(candidate)
    if not isinstance(value, dict):
        raise ValueError("response must be a JSON object")
    return value
