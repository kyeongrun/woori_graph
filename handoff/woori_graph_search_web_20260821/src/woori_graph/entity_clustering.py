"""Broad canonical-name clustering for reviewable entity dictionaries."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path
from typing import Any

from .extraction import CompletionClient
from .ids import stable_id
from .prompting import load_prompt_asset


ENTITY_NORMALIZATION_PROMPT = """다음은 법령 SVO에서 분리된 엔티티 후보 목록이다.
각 alias를 대표 canonical_name으로 정리하라. 엄밀한 온톨로지 판정이 아니라 검색용 사전 구축이므로
약간의 표기 차이와 의미 변형은 같은 항목으로 묶어도 된다.

규칙:
- 동일 기관·사람 직위·법령·문서·도메인 개념의 약칭, 띄어쓰기, 따옴표, 조사·어미 차이는 같은 canonical_name을 사용한다.
- 대표 정식 명칭을 알 수 있으면 사용한다. 예: `금감원` → `금융감독원`, `공정위` → `공정거래위원회`.
- canonical_name은 짧은 명사구여야 하며 문장이나 조건절을 만들지 않는다.
- 금액·기간·인원·직급·법령 근거처럼 핵심 엔티티를 한정하는 조건은 제거해도 된다.
- 소유·소속이 엔티티의 정체성을 구분하면 유지한다. 예: `금융지주회사의 자회사`를 문맥 없이 단순 `자회사`로 합치지 않는다.
- 서로 다른 기관, 서로 다른 직위, 서로 다른 법령은 합치지 않는다.
- `위원회`, `회사`, `담당부서`처럼 문서마다 지칭 대상이 달라질 수 있는 표현은 입력 이름을 유지한다.
- 정의된 용어의 `등`이나 복합 범위가 정체성에 중요하면 임의로 제거하지 않는다.
- 확신이 없으면 입력 alias 자체를 canonical_name으로 유지한다.
- 입력 alias를 누락하거나 수정하지 않는다.
- JSON만 반환한다.

반환 형식:
{"items": [{"alias": "입력 엔티티", "canonical_name": "대표 엔티티"}]}
"""

ENTITY_NORMALIZATION_PROMPT = load_prompt_asset("entity_normalize.ko.md")

DEFAULT_ENTITY_OVERRIDE_PATH = (
    Path(__file__).resolve().parents[2]
    / "config"
    / "entity_normalization_overrides.jsonl"
)


def propose_entity_mapping(
    entity_records: Sequence[dict[str, Any]],
    client: CompletionClient,
    *,
    batch_size: int = 50,
    workers: int = 4,
) -> tuple[dict[str, dict[str, str]], list[dict[str, Any]]]:
    """Map global entity surface forms to broad canonical names through the LLM."""

    records_by_name = {record["canonical_name"]: record for record in entity_records}
    names = sorted(records_by_name, key=lambda name: (-records_by_name[name].get("mention_count", 0), name))
    mapping: dict[str, dict[str, str]] = {}
    errors: list[dict[str, Any]] = []
    batches = [(start, names[start : start + batch_size]) for start in range(0, len(names), batch_size)]

    def normalize_batch(start: int, batch_names: list[str]):
        payload = [
            {
                "alias": name,
                "mention_count": records_by_name[name].get("mention_count", 0),
            }
            for name in batch_names
        ]
        response = client.complete(
            f"{ENTITY_NORMALIZATION_PROMPT}\n\n입력:\n{json.dumps(payload, ensure_ascii=False)}"
        )
        value = _parse_json_object(response)
        items = value.get("items")
        if not isinstance(items, list):
            raise ValueError("response must contain an items array")
        batch_mapping: dict[str, dict[str, str]] = {}
        for item in items:
            if not isinstance(item, Mapping):
                continue
            alias = item.get("alias")
            canonical_name = item.get("canonical_name")
            if alias in batch_names and isinstance(canonical_name, str) and canonical_name.strip():
                batch_mapping[alias] = {
                    "canonical_name": _clean_name(canonical_name),
                    "normalization_status": "llm_proposed",
                }
        return start, batch_names, batch_mapping

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(normalize_batch, start, batch_names): (start, batch_names)
            for start, batch_names in batches
        }
        for future in as_completed(futures):
            start, batch_names = futures[future]
            try:
                _, _, batch_mapping = future.result()
                mapping.update(batch_mapping)
            except Exception as exc:
                errors.append(
                    {
                        "batch_start": start,
                        "aliases": batch_names,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )

    for _, batch_names in batches:
        for name in batch_names:
            mapping.setdefault(
                name,
                {
                    "canonical_name": _clean_name(name),
                    "normalization_status": "fallback_raw_missing_proposal",
                },
            )
    return mapping, errors


def build_clustered_entity_dictionary(
    entity_records: Sequence[dict[str, Any]],
    mapping: Mapping[str, Mapping[str, str]],
    *,
    sample_limit: int = 5,
    canonical_overrides: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Merge source entity records by canonical name without document scope."""

    overrides = (
        dict(canonical_overrides)
        if canonical_overrides is not None
        else dict(load_default_entity_overrides())
    )
    buckets: dict[str, dict[str, Any]] = {}
    for record in entity_records:
        source_name = record["canonical_name"]
        proposal = mapping.get(source_name, {})
        if source_name in overrides:
            canonical_name = _clean_name(overrides[source_name])
            method = "explicit_entity_override"
        else:
            canonical_name = _clean_name(str(proposal.get("canonical_name", source_name)))
            method = str(proposal.get("normalization_status", "mapping_missing"))
        entity_id = stable_id("entity", canonical_name)
        bucket = buckets.setdefault(
            canonical_name,
            {
                "entity_id": entity_id,
                "canonical_name": canonical_name,
                "aliases": {},
                "sample_source_refs": [],
                "mention_count": 0,
                "normalization_methods": [],
            },
        )
        bucket["mention_count"] += int(record.get("mention_count", 0))
        if method not in bucket["normalization_methods"]:
            bucket["normalization_methods"].append(method)
        _extend_samples(bucket["sample_source_refs"], record.get("sample_source_refs", []), sample_limit)
        source_alias_count = sum(
            int(alias.get("mention_count", 0)) for alias in record.get("aliases", [])
        )
        direct_source_count = max(0, int(record.get("mention_count", 0)) - source_alias_count)
        alias_sources = [
            (
                source_name,
                direct_source_count,
                record.get("sample_source_refs", []),
            )
        ]
        for alias in record.get("aliases", []):
            alias_sources.append(
                (
                    alias.get("name", ""),
                    int(alias.get("mention_count", 0)),
                    alias.get("sample_source_refs", []),
                )
            )
        for alias_name, mention_count, samples in alias_sources:
            alias_name = alias_name.strip()
            if not alias_name:
                continue
            alias = bucket["aliases"].setdefault(
                alias_name,
                {
                    "name": alias_name,
                    "mention_count": 0,
                    "sample_source_refs": [],
                },
            )
            alias["mention_count"] += mention_count
            _extend_samples(alias["sample_source_refs"], samples, sample_limit)

    # Explicitly curated groups are dictionary knowledge, so retain every
    # configured alias even when extraction already split or shortened that
    # exact surface form in the current corpus.
    for alias_name, canonical_name in overrides.items():
        target = buckets.get(canonical_name)
        if target is None:
            continue
        for bucket in buckets.values():
            if bucket is not target:
                bucket["aliases"].pop(alias_name, None)
        target["aliases"].setdefault(
            alias_name,
            {
                "name": alias_name,
                "mention_count": 0,
                "sample_source_refs": [],
            },
        )

    output = []
    for bucket in buckets.values():
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
            key=lambda item: (
                not item["is_canonical"],
                -item["mention_count"],
                item["name"],
            )
        )
        bucket["aliases"] = aliases
        output.append(bucket)
    _remove_cross_entity_alias_conflicts(output)
    output.sort(key=lambda item: (item["canonical_name"], item["entity_id"]))
    return output


def entity_mapping_records(mapping: Mapping[str, Mapping[str, str]]) -> list[dict[str, str]]:
    return [{"alias": alias, **dict(value)} for alias, value in sorted(mapping.items())]


def entity_mapping_from_records(
    records: Iterable[Mapping[str, Any]],
) -> dict[str, dict[str, str]]:
    mapping: dict[str, dict[str, str]] = {}
    for record in records:
        alias = record.get("alias")
        canonical_name = record.get("canonical_name")
        if not isinstance(alias, str) or not alias.strip():
            raise ValueError("entity-map record must contain a non-empty alias")
        if not isinstance(canonical_name, str) or not canonical_name.strip():
            raise ValueError(f"entity-map alias {alias!r} has no canonical_name")
        mapping[alias.strip()] = {
            "canonical_name": _clean_name(canonical_name),
            "normalization_status": str(record.get("normalization_status", "review_map_input")),
        }
    return mapping


@lru_cache(maxsize=1)
def load_default_entity_overrides() -> dict[str, str]:
    """Load deterministic alias groups used after LLM normalization proposals."""

    if not DEFAULT_ENTITY_OVERRIDE_PATH.exists():
        return {}
    mapping: dict[str, str] = {}
    for line_number, line in enumerate(
        DEFAULT_ENTITY_OVERRIDE_PATH.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        record = json.loads(line)
        canonical_name = _clean_name(str(record.get("canonical_name", "")))
        aliases = record.get("aliases")
        if not canonical_name or not isinstance(aliases, list):
            raise ValueError(
                f"invalid entity override at line {line_number}: "
                "canonical_name and aliases are required"
            )
        for raw_alias in [canonical_name, *aliases]:
            alias = _clean_name(str(raw_alias))
            if not alias:
                continue
            existing = mapping.get(alias)
            if existing is not None and existing != canonical_name:
                raise ValueError(
                    f"conflicting entity override for {alias!r}: "
                    f"{existing!r} vs {canonical_name!r}"
                )
            mapping[alias] = canonical_name
    return mapping


def _clean_name(name: str) -> str:
    value = re.sub(r"\s+", " ", name).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1].strip()
    return value


def _extend_samples(target: list[dict[str, Any]], values: Iterable[dict[str, Any]], limit: int) -> None:
    for value in values:
        if value not in target:
            target.append(dict(value))
        if len(target) >= limit:
            return


def _remove_cross_entity_alias_conflicts(records: list[dict[str, Any]]) -> None:
    """Assign every alias string to exactly one canonical entity."""

    claims: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for record in records:
        for alias in record.get("aliases", []):
            claims.setdefault(alias["name"], []).append((record, alias))
    for alias_name, alias_claims in claims.items():
        if len(alias_claims) <= 1:
            continue
        winner_record, _ = min(
            alias_claims,
            key=lambda claim: (
                claim[0]["canonical_name"] != alias_name,
                -int(claim[1].get("mention_count", 0)),
                -int(claim[0].get("mention_count", 0)),
                claim[0]["canonical_name"],
            ),
        )
        for record, alias in alias_claims:
            if record is winner_record:
                continue
            record["aliases"].remove(alias)


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
