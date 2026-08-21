"""Assign one of the five released entity types after name normalization."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from .extraction import CompletionClient
from .prompting import load_prompt_asset


ENTITY_TYPES = (
    "ORGANIZATION",
    "PERSON",
    "LEGAL_INSTRUMENT",
    "CONCEPT",
    "OTHER",
)
ENTITY_TYPE_SET = frozenset(ENTITY_TYPES)
DEFAULT_ENTITY_TYPE_PROMPT = load_prompt_asset("entity_type_classify.ko.md")


class EntityTypeValidationError(ValueError):
    """Raised when a released entity dictionary has missing or invalid types."""


@dataclass(frozen=True)
class EntityTypeDecision:
    entity_type: str
    method: str


_LEGAL_INSTRUMENT_RE = re.compile(
    r"(?:법|법률|법령|시행령|시행규칙|규칙|규정|규약|조례|정관|고시|예규|훈령|"
    r"대통령령|총리령|부령)$"
)
_ORGANIZATION_RE = re.compile(
    r"(?:"
    r"위원회|이사회|주주총회|협의회|금융감독원|감독원|"
    r"회사|법인|기관|공공기관|금융기관|은행|저축은행|금고|대리점|지점|"
    r"거래소|협회|조합|연합회|재단|공단|공사|정부|법원|"
    r"검찰청|행정청|사무국|본부|센터|부서|사업자|업자|투자기구|"
    r"감사기구|기업집단|지방자치단체|단체|결제원|총회|국가"
    r")$"
)
_PERSON_RE = re.compile(
    r"(?:"
    r"준법감시인|감사위원|위원장|대표이사|사외이사|상근이사|비상근이사|"
    r"임원|직원|공무원|위원|이사|감사인|"
    r"담당자|책임자|대표자|신청인|신청자|소비자|고객|본인|대리인|"
    r"관계인|소유자|사용자|이용자|가입자|수익자|의뢰인|"
    r"근로자|종업원|채무자|채권자|투자자|주주|사원|조합원|"
    r"회계사|변호사|건축사|검사인|원장|기관장|부서장|팀장|실장|"
    r"본부장|회장|사장|행장|장관|총재|대통령|의장|"
    r"청산인|공직자|발행인|발기인|지배인|관리인|사용인|동일인|청구인|"
    r"보험중개사|손해사정사|보험계약자|채권추심자|공개매수자|직무대행자|"
    r"수탁자|위탁자|당사자|예탁자|질권자|회원|개인|누구든지|"
    r"담당관|감시인|단장|자|사람"
    r")$"
)
_CONCEPT_RE = re.compile(
    r"(?:"
    r"벌금|과태료|과징금|부담금|수수료|보험료|대금|금액|"
    r"개인정보|정보|자료|문서|서류|보고서|확인서|신청서|계획서|계약서|"
    r"의무|권리|책임|권한|업무|행위|조치|절차|방법|내용|목적|필요성|"
    r"지원|기준|요건|사유|기간|비율|범위|승인|허가|신고|보고|공시|공고|"
    r"감사|검사|심사|조사|제재|처분|경고|주의|정지|취소|해임|지정|등록|"
    r"신청|제출|작성|보관|예금|보험|대출|거래|계약|사업|주식|증권|"
    r"자산|채무|채권|손해|위험|통제|제도|정책|보호|관리|운영|"
    r"교육|평가|검증|결제|감리|기록|명령|결정|의결|통지|통보|"
    r"징역|벌금형|사실|서면|의견|결과|의결권|요구|인가|요청|사항|"
    r"이익|재산|비용|장부|조건|인력|명칭|금전|비밀|등기|설비|"
    r"상호|회의|자금|자본금|기금|보증금|포상금|보상금|구조금|번호|"
    r"상태표|금품|성명|보수|감봉|시정|"
    r"신탁|견책|민원|면직|신용|주소|청약|담보|어음|한도|합병|"
    r"권고|이의|재심의|체계|확인|기한|모집|변경|열람|직무|"
    r"증표|세부사항|세부 사항|방지체계|상품|출석|해산|사무|보완|"
    r"지급|부동산|사채|체결|계획|투자업|홈페이지|정직|수익권|"
    r"시기|시험|결의|가격|매매|심의|제공|서|금"
    r")$"
)

_ACTIVITY_CONCEPTS = frozenset({"투자", "출자", "융자"})


def infer_entity_type(
    canonical_name: str,
    aliases: Sequence[Mapping[str, Any] | str] = (),
) -> EntityTypeDecision:
    """Return a deterministic high-confidence type or explicit ``OTHER``."""

    names = [canonical_name]
    for alias in aliases:
        value = alias.get("name", "") if isinstance(alias, Mapping) else alias
        if isinstance(value, str) and value.strip() and value.strip() not in names:
            names.append(value.strip())

    for name in names:
        decision = _classify_surface(name.strip())
        if decision is not None:
            method = "canonical_rule" if name == canonical_name else "alias_rule"
            return EntityTypeDecision(decision, method)
    return EntityTypeDecision("OTHER", "fallback_other")


def build_entity_type_mapping(
    entity_records: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build a deterministic entity-id-to-type mapping."""

    mappings: list[dict[str, Any]] = []
    for record in entity_records:
        decision = infer_entity_type(
            str(record["canonical_name"]),
            record.get("aliases", []),
        )
        mappings.append(
            {
                "entity_id": str(record["entity_id"]),
                "canonical_name": str(record["canonical_name"]),
                "entity_type": decision.entity_type,
                "assignment_method": decision.method,
            }
        )
    return sorted(mappings, key=lambda item: (item["canonical_name"], item["entity_id"]))


def apply_entity_type_mapping(
    entity_records: Sequence[dict[str, Any]],
    mapping_records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Attach validated types without changing canonical names, aliases, or IDs."""

    mapping: dict[str, str] = {}
    for item in mapping_records:
        entity_id = str(item.get("entity_id", "")).strip()
        entity_type = str(item.get("entity_type", "")).strip().upper()
        if not entity_id or entity_type not in ENTITY_TYPE_SET:
            raise EntityTypeValidationError(
                "entity type mapping requires entity_id and one of "
                f"{list(ENTITY_TYPES)}"
            )
        previous = mapping.get(entity_id)
        if previous is not None and previous != entity_type:
            raise EntityTypeValidationError(
                f"conflicting entity types for {entity_id}: {previous} and {entity_type}"
            )
        mapping[entity_id] = entity_type

    source_ids = {str(item["entity_id"]) for item in entity_records}
    missing = sorted(source_ids - set(mapping))
    extra = sorted(set(mapping) - source_ids)
    if missing or extra:
        raise EntityTypeValidationError(
            f"entity type mapping coverage mismatch: missing={missing[:10]}, extra={extra[:10]}"
        )

    output: list[dict[str, Any]] = []
    for record in entity_records:
        item = dict(record)
        item["entity_type"] = mapping[str(record["entity_id"])]
        output.append(item)
    return output


def validate_typed_entity_dictionary(
    entity_records: Sequence[Mapping[str, Any]],
) -> None:
    """Require every released dictionary entity to carry an explicit valid type."""

    invalid = [
        {
            "entity_id": str(item.get("entity_id", "")),
            "canonical_name": str(item.get("canonical_name", "")),
            "entity_type": item.get("entity_type"),
        }
        for item in entity_records
        if item.get("entity_type") not in ENTITY_TYPE_SET
    ]
    if invalid:
        raise EntityTypeValidationError(
            f"released entity dictionary has {len(invalid)} missing or invalid entity types; "
            "run classify-entity-types before mapping or loading; "
            f"samples={invalid[:5]!r}"
        )


def propose_llm_entity_types(
    entity_records: Sequence[dict[str, Any]],
    client: CompletionClient,
    *,
    prompt_template: str = DEFAULT_ENTITY_TYPE_PROMPT,
    batch_size: int = 80,
    workers: int = 4,
    progress_callback: Callable[
        [list[dict[str, Any]], list[dict[str, Any]]], None
    ]
    | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Classify every supplied canonical entity with an LLM in bounded batches."""

    if batch_size < 1 or workers < 1:
        raise ValueError("batch_size and workers must be at least 1")
    batches = [
        list(entity_records[start : start + batch_size])
        for start in range(0, len(entity_records), batch_size)
    ]
    outputs: list[list[dict[str, Any]] | None] = [None] * len(batches)
    errors: list[dict[str, Any]] = []

    def process(index: int, batch: list[dict[str, Any]]) -> tuple[int, list[dict[str, Any]]]:
        compact = [
            {
                "entity_id": str(item["entity_id"]),
                "canonical_name": str(item["canonical_name"]),
                "aliases": [
                    str(alias.get("name", ""))
                    for alias in item.get("aliases", [])
                    if isinstance(alias, Mapping) and str(alias.get("name", "")).strip()
                ][:10],
            }
            for item in batch
        ]
        response = client.complete(
            f"{prompt_template}\n\n입력:\n{json.dumps({'entities': compact}, ensure_ascii=False)}"
        )
        return index, _parse_llm_types(response, compact)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(process, index, batch): (index, batch)
            for index, batch in enumerate(batches)
        }
        for future in as_completed(futures):
            index, batch = futures[future]
            try:
                output_index, values = future.result()
                outputs[output_index] = values
                if progress_callback:
                    progress_callback(values, [])
            except Exception as exc:
                error = {
                    "batch_index": index,
                    "entity_ids": [str(item["entity_id"]) for item in batch],
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
                errors.append(error)
                if progress_callback:
                    progress_callback([], [error])
    return [item for batch in outputs if batch for item in batch], sorted(
        errors, key=lambda item: item["batch_index"]
    )


def merge_entity_type_mappings(
    deterministic: Sequence[dict[str, Any]],
    llm_mapping: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Replace deterministic ``OTHER`` mappings only with valid LLM decisions."""

    llm_by_id = {str(item["entity_id"]): item for item in llm_mapping}
    output: list[dict[str, Any]] = []
    for item in deterministic:
        replacement = llm_by_id.get(str(item["entity_id"]))
        if item["entity_type"] == "OTHER" and replacement is not None:
            output.append(
                {
                    **item,
                    "entity_type": replacement["entity_type"],
                    "assignment_method": "llm",
                }
            )
        else:
            output.append(dict(item))
    return output


def audit_entity_types(
    entity_records: Sequence[Mapping[str, Any]],
    mapping_records: Sequence[Mapping[str, Any]],
    *,
    require_all_llm: bool = False,
) -> dict[str, Any]:
    source_ids = {str(item["entity_id"]) for item in entity_records}
    mapped_ids = [str(item.get("entity_id", "")) for item in mapping_records]
    types = [str(item.get("entity_type", "")) for item in mapping_records]
    checks = {
        "all_entities_mapped": source_ids == set(mapped_ids),
        "mapping_ids_unique": len(mapped_ids) == len(set(mapped_ids)),
        "all_entity_types_valid": all(value in ENTITY_TYPE_SET for value in types),
    }
    if require_all_llm:
        checks["all_entity_types_assigned_by_llm"] = (
            len(mapping_records) == len(entity_records)
            and all(str(item.get("assignment_method", "")) == "llm" for item in mapping_records)
        )
    return {
        "passed": all(checks.values()),
        "counts": {
            "entities": len(entity_records),
            "mapping_records": len(mapping_records),
            "entity_types": dict(sorted(Counter(types).items())),
            "assignment_methods": dict(
                sorted(
                    Counter(
                        str(item.get("assignment_method", ""))
                        for item in mapping_records
                    ).items()
                )
            ),
        },
        "checks": checks,
    }


def _classify_surface(name: str) -> str | None:
    compact = re.sub(r"\s+", " ", name).strip(" ,.;:()[]{}\"'")
    if not compact:
        return None
    base = re.sub(r"\s*등$", "", compact).strip()
    if base in _ACTIVITY_CONCEPTS:
        return "CONCEPT"
    if _LEGAL_INSTRUMENT_RE.search(base):
        return "LEGAL_INSTRUMENT"
    if _ORGANIZATION_RE.search(base):
        return "ORGANIZATION"
    if re.search(r"의\s*장$", base) or _PERSON_RE.search(base):
        return "PERSON"
    if _CONCEPT_RE.search(base):
        return "CONCEPT"
    return None


def _parse_llm_types(
    response_text: str,
    expected: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    candidate = response_text.strip()
    if candidate.startswith("```"):
        candidate = candidate.split("\n", maxsplit=1)[-1]
        if candidate.rstrip().endswith("```"):
            candidate = candidate.rstrip()[:-3]
    payload = json.loads(candidate)
    values = payload.get("entities") if isinstance(payload, dict) else None
    if not isinstance(values, list):
        raise ValueError("LLM response must contain an entities array")
    expected_by_id = {str(item["entity_id"]): item for item in expected}
    result: dict[str, dict[str, Any]] = {}
    for item in values:
        if not isinstance(item, Mapping):
            continue
        entity_id = str(item.get("entity_id", "")).strip()
        entity_type = str(item.get("entity_type", "")).strip().upper()
        if entity_id not in expected_by_id or entity_type not in ENTITY_TYPE_SET:
            continue
        result[entity_id] = {
            "entity_id": entity_id,
            "canonical_name": str(expected_by_id[entity_id]["canonical_name"]),
            "entity_type": entity_type,
            "assignment_method": "llm",
        }
    missing = sorted(set(expected_by_id) - set(result))
    if missing:
        raise ValueError(f"LLM omitted or invalidated {len(missing)} entity IDs")
    return [result[str(item["entity_id"])] for item in expected]
