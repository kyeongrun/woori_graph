"""Compress reviewed relation types into a small, closed discovery taxonomy."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .extraction import CompletionClient
from .ids import stable_id
from .prompting import load_prompt_asset


RELATION_TAXONOMY_PROMPT = """법령·내규 그래프 탐색에 사용할 폐쇄형 관계 분류체계를 설계한다.
입력은 60개 기준 문서에서 이미 추출·1차 정규화한 관계 타입과 출현 횟수다.

목표:
- 세밀한 법률 양태가 아니라 검색 연결성을 위한 넓은 핵심 행위군으로 묶는다.
- 의미가 조금 다르거나 부수 행위가 생략되어도 같은 탐색 결과로 찾는 편이 유용하면 합친다.
- 각 행위군은 긍정 이름과 부정 이름을 한 쌍으로 가진다.
- 의무·허용·가능·수동·능동·시제는 구분하지 않는다.
- 제출·송부·교부처럼 전달 성격이 가까운 행위, 실시·수행·시행처럼 실행 성격이 가까운 행위는 넓게 합칠 수 있다.
- 모든 입력을 반드시 어느 한 행위군으로 분류할 수 있어야 한다.
- 정말 별도 의미군을 만들 필요가 없는 드문 표현을 받을 수 있도록 넓은 `수행하다/수행하지않다` 행위군을 포함한다.
- 지정된 최대 행위군 수를 넘지 않는다.
- 이름은 짧고 일관된 한국어 동사형으로 작성한다.

반환 형식은 JSON 객체 하나다.
{"items":[{"positive_name":"대표 긍정 관계","negative_name":"대표 부정 관계","description":"이 군에 포함할 행위의 짧은 범위 설명"}]}
"""


RELATION_MAPPING_PROMPT = """입력 관계 타입을 제공된 폐쇄형 관계 분류체계에 전부 매핑한다.

규칙:
- 새 family_id나 새 canonical_name을 만들지 않는다.
- 입력 polarity는 바꾸지 않는다.
- 반대 방향의 대표명에 넣지 않는다. 예: `거절하다`→`승인하다`, `해임하다`→`임명하다`, `해지하다`→`계약하다`로 매핑하지 않는다. 더 넓더라도 방향이 맞는 제한·종료·반환 계열을 선택한다.
- 세밀한 법률 차이보다 그래프 탐색에서 함께 찾을 핵심 행위를 우선한다.
- 드문 표현도 누락하지 말고 가장 가까운 family_id 하나를 고른다.
- 원래 관계 타입 이름을 수정하지 않는다.
- JSON만 반환한다.

반환 형식:
{"items":[{"source_canonical_name":"입력 이름","family_id":"R001"}]}
"""

RELATION_TAXONOMY_PROMPT = load_prompt_asset("relation_taxonomy.ko.md")
RELATION_MAPPING_PROMPT = load_prompt_asset("relation_closed_map.ko.md")


_SEMANTIC_GUARD_RULES = (
    (re.compile(r"^.+등기(?:하다|하지않다)$"), "등록하다"),
    (re.compile(r"^(?:지정해제|철회|취소|해제)(?:하다|하지않다)$"), "취소하다"),
    (re.compile(r"^(?:해임|해지|폐지|삭제|상실|소멸|종료)(?:하다|하지않다)$"), "종료하다"),
    (re.compile(r"^반려(?:하다|하지않다)$"), "반환하다"),
    (re.compile(r"^누설(?:하다|하지않다)$"), "공개하다"),
    (re.compile(r"^(?:거절|거부|배제|제외|차단|금지)(?:하다|하지않다)$"), "제한하다"),
    (re.compile(r"^(?:선임|임명|위촉)(?:하다|하지않다)$"), "임명하다"),
)


def propose_relation_taxonomy(
    relation_records: Sequence[dict[str, Any]],
    client: CompletionClient,
    *,
    target_families: int = 45,
    inventory_batch_size: int = 160,
    workers: int = 4,
) -> list[dict[str, str]]:
    """Ask the LLM for at most ``target_families`` polarity-paired families.

    A large inventory is summarized in parallel first. Sending every source
    type in one request is slow on the closed-network model and can exceed the
    endpoint timeout before it starts returning tokens.
    """

    if not 1 <= target_families <= 50:
        raise ValueError("target_families must be between 1 and 50")
    if inventory_batch_size < 1 or workers < 1:
        raise ValueError("inventory_batch_size and workers must be at least 1")
    ordered = sorted(
        relation_records,
        key=lambda item: (-int(item.get("mention_count", 0)), item["canonical_name"]),
    )
    if len(ordered) <= inventory_batch_size:
        return _request_taxonomy(
            ordered,
            client,
            maximum_families=target_families,
        )

    batches = [
        ordered[start : start + inventory_batch_size]
        for start in range(0, len(ordered), inventory_batch_size)
    ]
    local_limit = min(25, target_families)
    local_taxonomies: list[list[dict[str, str]] | None] = [None] * len(batches)

    def summarize(batch_index: int, batch: list[dict[str, Any]]):
        return batch_index, _request_taxonomy(
            batch,
            client,
            maximum_families=local_limit,
        )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(summarize, batch_index, batch): batch_index
            for batch_index, batch in enumerate(batches)
        }
        for future in as_completed(futures):
            batch_index, taxonomy = future.result()
            local_taxonomies[batch_index] = taxonomy

    candidates: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for local_taxonomy in local_taxonomies:
        if local_taxonomy is None:
            raise RuntimeError("taxonomy summary batch did not return a result")
        for item in local_taxonomy:
            pair = (item["positive_name"], item["negative_name"])
            if pair in seen:
                continue
            seen.add(pair)
            candidates.append(item)

    candidate_inventory = [
        {
            "canonical_name": (
                f"{item['positive_name']} / {item['negative_name']} / {item['description']}"
            ),
            "polarity": "PAIRED_CANDIDATE",
            "mention_count": 0,
        }
        for item in candidates
    ]
    return _request_taxonomy(
        candidate_inventory,
        client,
        maximum_families=target_families,
        consolidation=True,
    )


def _request_taxonomy(
    relation_records: Sequence[dict[str, Any]],
    client: CompletionClient,
    *,
    maximum_families: int,
    consolidation: bool = False,
) -> list[dict[str, str]]:
    inventory = "\n".join(
        f"{record['canonical_name']} | {record['polarity']} | {record.get('mention_count', 0)}"
        for record in relation_records
    )
    phase_instruction = (
        "아래 입력은 부분 목록에서 제안된 후보 행위군이다. 중복·유사 후보를 다시 합쳐 "
        "전체가 함께 사용할 최종 분류체계로 통합하라."
        if consolidation
        else "아래 입력 관계들을 빠짐없이 포괄하는 후보 행위군을 제안하라."
    )
    response = client.complete(
        f"{RELATION_TAXONOMY_PROMPT}\n\n{phase_instruction}"
        f"\n최대 행위군 수: {maximum_families}\n\n입력:\n{inventory}"
    )
    value = _parse_json_object(response)
    items = value.get("items")
    if not isinstance(items, list):
        raise ValueError("taxonomy response must contain an items array")

    taxonomy: list[dict[str, str]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for item in items:
        if not isinstance(item, Mapping):
            continue
        positive_name = _required_text(item, "positive_name")
        negative_name = _required_text(item, "negative_name")
        description = _required_text(item, "description")
        pair = (positive_name, negative_name)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        taxonomy.append(
            {
                "family_id": f"R{len(taxonomy) + 1:03d}",
                "positive_name": positive_name,
                "negative_name": negative_name,
                "description": description,
            }
        )
    if not taxonomy:
        raise ValueError("taxonomy response did not contain any valid families")
    if len(taxonomy) > maximum_families:
        raise ValueError(
            f"taxonomy returned {len(taxonomy)} families; maximum is {maximum_families}"
        )
    if len({item["positive_name"] for item in taxonomy}) != len(taxonomy):
        raise ValueError("taxonomy positive names must be unique")
    if len({item["negative_name"] for item in taxonomy}) != len(taxonomy):
        raise ValueError("taxonomy negative names must be unique")
    return taxonomy


def propose_closed_relation_mapping(
    relation_records: Sequence[dict[str, Any]],
    taxonomy: Sequence[dict[str, str]],
    client: CompletionClient,
    *,
    batch_size: int = 50,
    workers: int = 4,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Map every source relation type to one supplied family."""

    if batch_size < 1 or workers < 1:
        raise ValueError("batch_size and workers must be at least 1")
    family_by_id = {item["family_id"]: item for item in taxonomy}
    taxonomy_payload = json.dumps(list(taxonomy), ensure_ascii=False, separators=(",", ":"))
    ordered = sorted(
        relation_records,
        key=lambda item: (-int(item.get("mention_count", 0)), item["canonical_name"]),
    )
    batches = [ordered[start : start + batch_size] for start in range(0, len(ordered), batch_size)]
    mappings: dict[tuple[str, str], dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []

    def map_batch(batch_index: int, batch: list[dict[str, Any]]):
        source_payload = [
            {
                "source_canonical_name": record["canonical_name"],
                "polarity": record["polarity"],
                "mention_count": record.get("mention_count", 0),
                "alias_examples": [
                    alias.get("name") for alias in record.get("aliases", [])[:3]
                ],
            }
            for record in batch
        ]
        response = client.complete(
            f"{RELATION_MAPPING_PROMPT}\n\n분류체계:\n{taxonomy_payload}"
            f"\n\n입력:\n{json.dumps(source_payload, ensure_ascii=False, separators=(',', ':'))}"
        )
        value = _parse_json_object(response)
        items = value.get("items")
        if not isinstance(items, list):
            raise ValueError("mapping response must contain an items array")
        source_by_name = {record["canonical_name"]: record for record in batch}
        batch_mapping: dict[tuple[str, str], dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, Mapping):
                continue
            source_name = item.get("source_canonical_name")
            family_id = item.get("family_id")
            if source_name not in source_by_name or family_id not in family_by_id:
                continue
            source = source_by_name[source_name]
            polarity = source["polarity"]
            family = family_by_id[family_id]
            batch_mapping[(source_name, polarity)] = {
                "source_relation_type_id": source["relation_type_id"],
                "source_canonical_name": source_name,
                "source_polarity": polarity,
                "target_family_id": family_id,
                "target_canonical_name": (
                    family["positive_name"]
                    if polarity == "POSITIVE"
                    else family["negative_name"]
                ),
                "mention_count": int(source.get("mention_count", 0)),
                "mapping_status": "llm_closed_taxonomy",
            }
        expected = {(record["canonical_name"], record["polarity"]) for record in batch}
        missing = sorted(expected - set(batch_mapping))
        if missing:
            raise ValueError(f"mapping response omitted {len(missing)} source types")
        return batch_index, batch_mapping

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(map_batch, batch_index, batch): (batch_index, batch)
            for batch_index, batch in enumerate(batches)
        }
        for future in as_completed(futures):
            batch_index, batch = futures[future]
            try:
                _, batch_mapping = future.result()
                mappings.update(batch_mapping)
            except Exception as exc:
                errors.append(
                    {
                        "batch_index": batch_index,
                        "source_canonical_names": [record["canonical_name"] for record in batch],
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
    guarded = sanitize_closed_relation_mapping(list(mappings.values()), taxonomy)
    return sorted(
        guarded,
        key=lambda item: (item["source_canonical_name"], item["source_polarity"]),
    ), sorted(errors, key=lambda item: item["batch_index"])


def sanitize_closed_relation_mapping(
    mapping_records: Sequence[dict[str, Any]],
    taxonomy: Sequence[dict[str, str]],
) -> list[dict[str, Any]]:
    """Correct a small set of direction-reversing broad-family mappings."""

    family_by_positive_name = {item["positive_name"]: item for item in taxonomy}
    sanitized: list[dict[str, Any]] = []
    for record in mapping_records:
        item = dict(record)
        source_name = str(item["source_canonical_name"])
        for pattern, positive_name in _SEMANTIC_GUARD_RULES:
            if not pattern.search(source_name):
                continue
            family = family_by_positive_name.get(positive_name)
            if family is None:
                break
            polarity = item["source_polarity"]
            target_name = (
                family["positive_name"]
                if polarity == "POSITIVE"
                else family["negative_name"]
            )
            if item.get("target_family_id") != family["family_id"]:
                item["target_family_id"] = family["family_id"]
                item["target_canonical_name"] = target_name
                item["mapping_status"] = "rule_semantic_direction_guard"
            break
        sanitized.append(item)
    return sanitized


def build_closed_relation_dictionary(
    relation_records: Sequence[dict[str, Any]],
    mapping_records: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Aggregate source dictionaries under their closed-taxonomy targets."""

    mapping = {
        (item["source_canonical_name"], item["source_polarity"]): item
        for item in mapping_records
    }
    source_keys = {(item["canonical_name"], item["polarity"]) for item in relation_records}
    missing = sorted(source_keys - set(mapping))
    if missing:
        raise ValueError(f"closed relation mapping is missing {len(missing)} source types")

    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for source in relation_records:
        key = (source["canonical_name"], source["polarity"])
        mapped = mapping[key]
        canonical_name = mapped["target_canonical_name"]
        polarity = source["polarity"]
        target_key = (canonical_name, polarity)
        bucket = buckets.setdefault(
            target_key,
            {
                "canonical_name": canonical_name,
                "aliases": {},
                "relation_type_id": stable_id("relation_type", canonical_name),
                "mention_count": 0,
                "polarity": polarity,
                "source_relation_type_count": 0,
            },
        )
        bucket["mention_count"] += int(source.get("mention_count", 0))
        bucket["source_relation_type_count"] += 1
        for alias in source.get("aliases", []):
            name = alias.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            alias_bucket = bucket["aliases"].setdefault(
                name,
                {"name": name, "mention_count": 0, "sample_source_refs": []},
            )
            alias_bucket["mention_count"] += int(alias.get("mention_count", 0))
            for sample in alias.get("sample_source_refs", []):
                if sample not in alias_bucket["sample_source_refs"]:
                    alias_bucket["sample_source_refs"].append(sample)
        bucket["aliases"].setdefault(
            canonical_name,
            {"name": canonical_name, "mention_count": 0, "sample_source_refs": []},
        )

    output: list[dict[str, Any]] = []
    for bucket in buckets.values():
        aliases = list(bucket["aliases"].values())
        for alias in aliases:
            alias["is_canonical"] = alias["name"] == bucket["canonical_name"]
        aliases.sort(
            key=lambda item: (not item["is_canonical"], -item["mention_count"], item["name"])
        )
        bucket["aliases"] = aliases
        output.append(bucket)
    output.sort(key=lambda item: (item["canonical_name"], item["polarity"]))
    return output


def audit_closed_relation_dictionary(
    source_records: Sequence[dict[str, Any]],
    mapping_records: Sequence[dict[str, Any]],
    output_records: Sequence[dict[str, Any]],
    *,
    maximum_relation_types: int = 100,
) -> dict[str, Any]:
    source_keys = {(item["canonical_name"], item["polarity"]) for item in source_records}
    mapped_keys = {
        (item["source_canonical_name"], item["source_polarity"])
        for item in mapping_records
    }
    source_mentions = sum(int(item.get("mention_count", 0)) for item in source_records)
    output_mentions = sum(int(item.get("mention_count", 0)) for item in output_records)
    checks = {
        "all_source_types_mapped": source_keys == mapped_keys,
        "mapping_is_one_to_one_per_source_type": len(mapping_records) == len(mapped_keys),
        "relation_type_limit_met": len(output_records) <= maximum_relation_types,
        "mention_count_preserved": source_mentions == output_mentions,
        "output_relation_type_ids_unique": len(
            {item["relation_type_id"] for item in output_records}
        )
        == len(output_records),
        "all_outputs_have_canonical_alias": all(
            any(
                alias.get("name") == item["canonical_name"] and alias.get("is_canonical") is True
                for alias in item.get("aliases", [])
            )
            for item in output_records
        ),
    }
    return {
        "passed": all(checks.values()),
        "counts": {
            "source_relation_types": len(source_records),
            "mapped_source_relation_types": len(mapped_keys),
            "output_relation_types": len(output_records),
            "source_mentions": source_mentions,
            "output_mentions": output_mentions,
        },
        "checks": checks,
        "unmapped_source_types": [
            {"canonical_name": name, "polarity": polarity}
            for name, polarity in sorted(source_keys - mapped_keys)
        ],
    }


def _parse_json_object(response_text: str) -> dict[str, Any]:
    candidate = response_text.strip()
    if candidate.startswith("```"):
        candidate = candidate.split("\n", maxsplit=1)[-1]
        if candidate.rstrip().endswith("```"):
            candidate = candidate.rstrip()[:-3]
    value = json.loads(candidate)
    if not isinstance(value, dict):
        raise ValueError("LLM response must be a JSON object")
    return value


def _required_text(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise ValueError(f"taxonomy item requires non-empty {key}")
    return item.strip()
