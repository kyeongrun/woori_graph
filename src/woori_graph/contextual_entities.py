"""Context-aware second-pass normalization for entity name dictionaries."""

from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from .entity_clustering import build_clustered_entity_dictionary
from .extraction import CompletionClient
from .prompting import load_prompt_asset


CONTEXTUAL_ENTITY_NORMALIZATION_PROMPT = """법령·내규 SVO의 엔티티 이름 사전을 검색용 canonical name으로 추가 정규화한다.
현재 이름, 그 이름에 포함된 원표현 alias, 실제 문서 문맥을 함께 보고 각 입력을 독립적으로 정규화하라.

규칙:
- 대표 이름은 짧고 검색 가능한 명사구로 만든다.
- 같은 기관·직위·법령·문서·개념의 약칭, 띄어쓰기, 따옴표, 조사 차이는 같은 canonical_name으로 합친다.
- 금액·기간·인원·연령·촌수·급수·비율·횟수 등 부수 조건은 제거하고 핵심 명사만 남긴다.
- `X법에 따른 A`, `X법 제N조에 따라 지정된 A`, `제N조에 따른 A`, `대통령령으로 정하는 A`에서 법령·조문이 A의 근거 수식어이면 A만 남긴다.
- 법령이나 조문 자체가 실제 endpoint이면 조·항·호를 제거하고 정식 법령명으로 합친다. `법 제N조`처럼 현재 문서를 가리키면 문맥의 document_title을 사용한다.
- `제3자`, `1차 시험`, 고유 상품명처럼 숫자가 정체성의 일부이면 숫자를 제거하지 않는다.
- `금융지주회사의 자회사`, `회사의 감사`처럼 소유·소속 문맥이 다른 대상을 구분하는 데 필요하면 유지한다.
- 조건을 제거한 결과가 `자`, `사람`, `사항`, `내용`, `대상`, `업무`, `행위`, `것`, `경우`, `금액`, `기간`, `비율` 같은 일반명사만 남는다면 그렇게 축약하지 않는다. 문맥에서 구체적인 핵심 엔티티를 찾고, 찾지 못하면 현재 이름을 유지한다.
- `제N조에 따른 A`, `법 제N조에 따라 지정된 A`에서 실제 endpoint는 A이지 법령 조문이 아니다. A가 존재하는 입력을 법령명이나 법령 조문으로 바꾸지 않는다.
- 입력 자체가 `법 제N조`, `「A법」 제N조`처럼 법령·조문만을 가리키는 경우에만 정식 법령명을 canonical_name으로 사용하고 조·항·호는 제거한다.
- 서로 다른 기관, 서로 다른 직위, 서로 다른 법령을 합치지 않는다.
- 문장·조건절·행위 표현을 canonical_name으로 만들지 않는다.
- 원문만으로 더 줄이면 의미가 달라질 수 있으면 현재 이름을 유지한다.
- 모든 입력 source_entity_id를 정확히 한 번 반환하고 새 ID를 만들지 않는다.
- JSON만 반환한다.

반환 형식:
{"items":[{"source_entity_id":"입력 ID","canonical_name":"정규화 이름"}]}
"""

# Runtime source of truth for the context-aware second pass.
CONTEXTUAL_ENTITY_NORMALIZATION_PROMPT = load_prompt_asset(
    "entity_contextual_normalize.ko.md"
)

_CONTEXTUAL_CANDIDATE_RE = re.compile(
    r"\d|[「」『』]|에\s*따른|에\s*따라|으로\s*정하는|"
    r"이내|이하|이상|초과|미만|동안|부터|까지"
)
_LEXICAL_PUNCTUATION = " \t\r\n\"'“”‘’「」『』·ㆍ・,.()[]{}"
_GENERIC_ONLY_NAMES = {
    "자",
    "사람",
    "사항",
    "내용",
    "대상",
    "업무",
    "행위",
    "것",
    "경우",
    "금액",
    "기간",
    "비율",
    "횟수",
    "범위",
}
_LAW_SECTION_NAME_RE = re.compile(
    r"^(?P<title>.+?(?:법률|법|시행령|시행규칙|규정|기준))\s+제\d+조(?:의\d+)?"
)
_BARE_LOCAL_SECTION_RE = re.compile(
    r"^(?:법\s*)?제\d+조(?:의\d+)?(?:제\d+항)?(?:제\d+호)?(?:[가-하]목)?"
    r"(?:\s*(?:전단|후단|단서|본문))?"
    r"(?:\s*(?:・|및|부터|까지|제|\d|항|호|목|의|\s|규정))*$"
)
_BARE_NAMED_SECTION_RE = re.compile(
    r"^[「『]?(?P<title>.+?(?:법률|법|시행령|시행규칙|규정|기준))[」』]?\s+"
    r"제\d+조(?:의\d+)?(?:제\d+항)?(?:제\d+호)?(?:[가-하]목)?"
    r"(?:\s*(?:전단|후단|단서|본문))?$"
)
_QUALIFIED_ENDPOINT_RE = re.compile(r"에\s*(?:따른|따라)\s*(?P<tail>.+)$")
_QUALIFIER_WITHOUT_TAIL_RE = re.compile(
    r"에\s*해당하는|각\s*호의\s*어느\s*하나에|에서\s*[「『\"'“”]?"
)
_LEADING_QUALIFIER_PARTICIPLE_RE = re.compile(
    r"^(?:지정된|설립된|등록된|작성된|제출된|발행된|인가받은|허가받은)\s+"
)


def needs_contextual_entity_normalization(name: str) -> bool:
    """Select names likely to contain conditions, citations, or clause text."""

    return len(name) >= 12 or bool(_CONTEXTUAL_CANDIDATE_RE.search(name))


def entity_lexical_key(name: str) -> str:
    """Return a comparison key for spacing/quote-only duplicate discovery."""

    table = str.maketrans("", "", _LEXICAL_PUNCTUATION)
    return unicodedata.normalize("NFKC", name).translate(table)


def sanitize_contextual_entity_mapping(
    mapping_records: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Apply deterministic semantic guards to LLM entity-name proposals.

    The LLM is allowed to shorten conditions and citations, but it must not
    erase the endpoint into a generic placeholder or reinterpret a qualified
    endpoint as the legal citation that merely modifies it.
    """

    sanitized: list[dict[str, Any]] = []
    for record in mapping_records:
        item = dict(record)
        source_name = str(item["source_canonical_name"]).strip()
        canonical_name = str(item["canonical_name"]).strip()

        if canonical_name in _GENERIC_ONLY_NAMES and source_name != canonical_name:
            item["canonical_name"] = source_name
            item["normalization_status"] = "guarded_generic_only_result"
            sanitized.append(item)
            continue

        law_match = _LAW_SECTION_NAME_RE.match(canonical_name)
        if law_match:
            qualified_tail = _qualified_endpoint_tail(source_name)
            if qualified_tail:
                item["canonical_name"] = qualified_tail
                item["normalization_status"] = "rule_qualified_endpoint_not_law"
            elif _QUALIFIER_WITHOUT_TAIL_RE.search(source_name):
                item["canonical_name"] = source_name
                item["normalization_status"] = "guarded_qualified_endpoint_not_law"
            elif _is_bare_law_citation(source_name):
                item["canonical_name"] = law_match.group("title").strip("「」『』 ")
                item["normalization_status"] = "rule_bare_citation_to_law_name"

        sanitized.append(item)
    return sanitized


def is_acceptable_llm_canonical_mapping(record: Mapping[str, Any]) -> bool:
    """Validate an LLM name without replacing the model's final decision."""

    source_name = str(record.get("source_canonical_name", "")).strip()
    canonical_name = str(record.get("canonical_name", "")).strip()
    if not source_name or not canonical_name:
        return False
    return not (
        canonical_name in _GENERIC_ONLY_NAMES and source_name != canonical_name
    )


def rekey_llm_mapping_by_source_name(
    entity_records: Sequence[Mapping[str, Any]],
    prior_mapping_records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Reuse an LLM decision for the identical source name under a new source ID."""

    prior_by_name = {
        str(item.get("source_canonical_name", "")).strip(): item
        for item in prior_mapping_records
        if str(item.get("normalization_status", "")).startswith("llm_")
        and is_acceptable_llm_canonical_mapping(item)
    }
    output: list[dict[str, Any]] = []
    for entity in entity_records:
        source_name = str(entity.get("canonical_name", "")).strip()
        prior = prior_by_name.get(source_name)
        if prior is None:
            continue
        output.append(
            {
                "source_entity_id": str(entity["entity_id"]),
                "source_canonical_name": source_name,
                "canonical_name": str(prior["canonical_name"]),
                "normalization_status": "llm_canonical_name_rekeyed",
            }
        )
    return sorted(output, key=lambda item: item["source_canonical_name"])


def _qualified_endpoint_tail(source_name: str) -> str | None:
    match = _QUALIFIED_ENDPOINT_RE.search(source_name)
    if not match:
        return None
    tail = _LEADING_QUALIFIER_PARTICIPLE_RE.sub("", match.group("tail").strip())
    if not tail or tail in _GENERIC_ONLY_NAMES:
        return None
    return tail


def _is_bare_law_citation(source_name: str) -> bool:
    return bool(
        _BARE_LOCAL_SECTION_RE.fullmatch(source_name)
        or _BARE_NAMED_SECTION_RE.fullmatch(source_name)
    )


def propose_contextual_entity_mapping(
    entity_records: Sequence[dict[str, Any]],
    raw_records: Sequence[dict[str, Any]],
    client: CompletionClient,
    *,
    batch_size: int = 40,
    workers: int = 4,
    sample_limit: int = 2,
    prompt_template: str = CONTEXTUAL_ENTITY_NORMALIZATION_PROMPT,
    require_all_llm: bool = False,
    rejected_proposals: Mapping[str, Sequence[str]] | None = None,
    progress_callback: Callable[
        [list[dict[str, Any]], list[dict[str, Any]]], None
    ]
    | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Normalize global entity names with their original SVO contexts."""

    if batch_size < 1 or workers < 1 or sample_limit < 1:
        raise ValueError("batch_size, workers, and sample_limit must be at least 1")
    evidence = _entity_evidence(raw_records, sample_limit=sample_limit)
    global_records = sorted(entity_records, key=lambda item: item["canonical_name"])
    batches = [
        global_records[start : start + batch_size]
        for start in range(0, len(global_records), batch_size)
    ]
    mappings: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []

    def normalize_batch(batch_index: int, batch: list[dict[str, Any]]):
        payload = []
        for record in batch:
            alias_names = [
                alias.get("name")
                for alias in record.get("aliases", [])
                if isinstance(alias.get("name"), str)
            ]
            names = list(dict.fromkeys([record["canonical_name"], *alias_names]))
            samples: list[dict[str, Any]] = []
            for name in names:
                for sample in evidence.get(name, []):
                    if sample not in samples:
                        samples.append(sample)
                    if len(samples) >= sample_limit:
                        break
                if len(samples) >= sample_limit:
                    break
            payload_item = {
                    "source_entity_id": record["entity_id"],
                    "current_canonical_name": record["canonical_name"],
                    "aliases": names[:10],
                    "mention_count": int(record.get("mention_count", 0)),
                    "samples": samples,
                }
            rejected = list((rejected_proposals or {}).get(record["entity_id"], ()))
            if rejected:
                payload_item["rejected_canonical_names"] = rejected
            payload.append(payload_item)
        response = client.complete(
            f"{prompt_template}\n\n입력:\n"
            f"{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
        )
        value = _parse_json_object(response)
        items = value.get("items")
        if not isinstance(items, list):
            raise ValueError("entity mapping response must contain an items array")
        source_by_id = {record["entity_id"]: record for record in batch}
        batch_mapping: dict[str, dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, Mapping):
                continue
            source_entity_id = item.get("source_entity_id")
            canonical_name = item.get("canonical_name")
            if (
                source_entity_id in source_by_id
                and isinstance(canonical_name, str)
                and canonical_name.strip()
            ):
                source = source_by_id[source_entity_id]
                batch_mapping[source_entity_id] = {
                    "source_entity_id": source_entity_id,
                    "source_canonical_name": source["canonical_name"],
                    "canonical_name": canonical_name.strip(),
                    "normalization_status": "llm_canonical_name",
                }
        expected = set(source_by_id)
        missing = expected - set(batch_mapping)
        if missing:
            raise ValueError(f"entity mapping response omitted {len(missing)} source entities")
        return batch_index, batch_mapping

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(normalize_batch, batch_index, batch): (batch_index, batch)
            for batch_index, batch in enumerate(batches)
        }
        for future in as_completed(futures):
            batch_index, batch = futures[future]
            try:
                _, batch_mapping = future.result()
                mappings.update(batch_mapping)
                if progress_callback:
                    progress_callback(list(batch_mapping.values()), [])
            except Exception as exc:
                error = {
                    "batch_index": batch_index,
                    "source_entity_ids": [record["entity_id"] for record in batch],
                    "source_canonical_names": [record["canonical_name"] for record in batch],
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
                errors.append(error)
                if require_all_llm:
                    if progress_callback:
                        progress_callback([], [error])
                    continue
                fallback_mappings = []
                for record in batch:
                    fallback = {
                        "source_entity_id": record["entity_id"],
                        "source_canonical_name": record["canonical_name"],
                        "canonical_name": record["canonical_name"],
                        "normalization_status": "fallback_source_name_batch_error",
                    }
                    mappings[record["entity_id"]] = fallback
                    fallback_mappings.append(fallback)
                if progress_callback:
                    progress_callback(fallback_mappings, [error])

    sanitized = (
        list(mappings.values())
        if require_all_llm
        else sanitize_contextual_entity_mapping(list(mappings.values()))
    )
    return sorted(
        sanitized, key=lambda item: item["source_canonical_name"]
    ), sorted(errors, key=lambda item: item["batch_index"])


def build_contextually_normalized_entity_dictionary(
    entity_records: Sequence[dict[str, Any]],
    mapping_records: Sequence[dict[str, Any]],
    *,
    sample_limit: int = 5,
    canonical_overrides: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    mapping = {
        item["source_canonical_name"]: {
            "canonical_name": item["canonical_name"],
            "normalization_status": item["normalization_status"],
        }
        for item in mapping_records
    }
    return build_clustered_entity_dictionary(
        entity_records,
        mapping,
        sample_limit=sample_limit,
        canonical_overrides=canonical_overrides,
    )


def audit_contextual_entity_dictionary(
    source_records: Sequence[dict[str, Any]],
    mapping_records: Sequence[dict[str, Any]],
    output_records: Sequence[dict[str, Any]],
    *,
    require_all_llm: bool = False,
) -> dict[str, Any]:
    source_entity_ids = {item["entity_id"] for item in source_records}
    mapped_ids = {item["source_entity_id"] for item in mapping_records}
    source_mentions = sum(int(item.get("mention_count", 0)) for item in source_records)
    output_mentions = sum(int(item.get("mention_count", 0)) for item in output_records)
    fallback_count = sum(
        item.get("normalization_status", "").startswith("fallback")
        for item in mapping_records
    )
    guarded_count = sum(
        item.get("normalization_status", "").startswith("guarded")
        for item in mapping_records
    )
    llm_mapping_count = sum(
        str(item.get("normalization_status", "")).startswith("llm_")
        for item in mapping_records
    )
    introduced_generic_names = [
        item["source_entity_id"]
        for item in mapping_records
        if not is_acceptable_llm_canonical_mapping(item)
    ]
    checks = {
        "all_source_entities_mapped": source_entity_ids == mapped_ids,
        "mapping_is_one_to_one_per_source_entity": len(mapping_records) == len(mapped_ids),
        "mention_count_preserved": source_mentions == output_mentions,
        "output_entity_ids_unique": len({item["entity_id"] for item in output_records})
        == len(output_records),
        "output_not_larger_than_source": len(output_records) <= len(source_records),
        "no_generic_only_name_introduced": not introduced_generic_names,
    }
    if require_all_llm:
        checks["all_canonical_names_selected_by_llm"] = (
            len(mapping_records) == len(source_records)
            and llm_mapping_count == len(mapping_records)
        )
    return {
        "passed": all(checks.values()),
        "counts": {
            "source_entities": len(source_records),
            "source_entities_for_mapping": len(source_entity_ids),
            "mapped_source_entities": len(mapped_ids),
            "output_entities": len(output_records),
            "reduction": len(source_records) - len(output_records),
            "changed_names": sum(
                item["source_canonical_name"] != item["canonical_name"]
                for item in mapping_records
            ),
            "fallback_mappings": fallback_count,
            "guarded_mappings": guarded_count,
            "llm_mappings": llm_mapping_count,
            "source_mentions": source_mentions,
            "output_mentions": output_mentions,
        },
        "checks": checks,
    }


def _entity_evidence(
    raw_records: Sequence[dict[str, Any]], *, sample_limit: int
) -> dict[str, list[dict[str, Any]]]:
    evidence: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in raw_records:
        for relation in record.get("relations", []):
            for role, key in (("subject", "subject"), ("object", "object")):
                name = relation.get(key)
                if not isinstance(name, str) or len(evidence[name]) >= sample_limit:
                    continue
                sample = {
                    "document_title": record.get("document_title"),
                    "source_ref": record.get("source_ref"),
                    "role": role,
                    "context_text": str(record.get("context_text", ""))[:500],
                    "unit_text": str(record.get("unit_text", ""))[:500],
                }
                if sample not in evidence[name]:
                    evidence[name].append(sample)
    return evidence


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
