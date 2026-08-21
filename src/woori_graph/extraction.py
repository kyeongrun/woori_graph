"""Raw SVO extraction through a local OpenAI-compatible chat endpoint."""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from .ids import stable_id
from .models import RawRelation, SemanticUnit
from .prompting import load_prompt_asset


DEFAULT_RAW_SVO_PROMPT = """당신은 법령·내규 원문에서 명시적으로 표현된 원시 SVO를 추출한다.

반드시 다음 규칙을 지켜라.
1. unit_text 및 제공된 context_text에서 확인되는 표현만 사용한다. 추론, 법률 해석, 도메인 판정, 관계 타입 정규화, 의무·허용·금지 분류를 하지 않는다.
2. subject와 object는 원문에서 확인되는 핵심 엔티티 표현만 반환한다. alias나 동의어로 정규화하지는 않되 조건, 금액, 기간, 비율, 횟수, 범위, 자격, 촌수, 인원수, 연령, 직급·등급·급수, 법령·조문 근거 수식어는 endpoint에서 제거한다. predicate는 원문 행위 표현을 유지하고 임의로 일반화하지 않는다. 상대 endpoint가 명시되지 않은 단순 상태·서술에는 SVO를 만들지 않는다.
2-1. `회사`, `위원회`, `임원`, `담당자`, `담당부서`, `대표자`처럼 원문에 일반명으로 적힌 endpoint를 문맥의 정식 회사명·기관명·직위명으로 바꾸지 않는다. 원문 표현 그대로 반환한다. 문서별 scope나 alias 해소는 하지 않는다.
3. context_text와 unit_text는 하나의 의미 단위다. 상위 본문의 주어·술어와 unit_text의 말단 호·목 명사구를 결합해 SVO를 추출할 수 있다. 예: `위원회는 다음 각 호의 업무를 처리한다`와 `신고 접수`는 `위원회 / 처리한다 / 신고 접수`로 추출한다.
3-1. 문장 안의 관형절·인용절·내포절에도 명시적인 행위자와 대상이 있으면 그 관계를 빠뜨리지 않는다. 예: `공공기관의 장이 실시한 자체감사의 결과를 감사원이 심사한다`에서는 `공공기관의 장 / 실시한 / 자체감사`와 `감사원 / 심사한다 / 자체감사`를 각각 추출한다.
3-2. 기간·심사명 같은 비행위 명사구가 문법상 주어처럼 보이더라도, 같은 의미 단위 안에 실제 실시 주체가 명시되어 있으면 그 주체를 subject로 사용한다. 실제 주체를 찾을 수 없거나 여러 후보 중 결정할 수 없으면 추측하여 관계를 만들지 않는다.
4. 단, context_text에만 있는 관계를 별도 SVO로 반복 추출하거나, object를 `각 호`, `사항`처럼 일반명사로 반환하지 않는다.
5. 조건, 기간, 금액, 비율, 횟수, 범위, 시행일, 상세 절차, 촌수, 인원수, 연령, 직급·등급·급수는 독립 endpoint나 관계로 만들지 않고 핵심 엔티티를 수식하는 부분도 endpoint에서 제거한다. `이상`, `이하`, `초과`, `미만`, `이내`, `간`, `동안`, `범위에서`, `부터`, `까지`와 결합한 수치 조건을 제거하고 뒤의 핵심 명사를 남긴다. 예: `3천만원 이하의 벌금`→`벌금`.
5-1. 조건을 제거한 뒤 endpoint에 핵심 명사가 남지 않고 `1년`, `15억원`, `5명`, `금액`, `기간`, `비율`, `횟수` 같은 조건값만 남으면 그 관계는 만들지 않는다.
5-1. `X법에 따른 A`, `X법 제N조에 따라 지정된 A`, `제N조에 따른 A`에서 법령명·조문·`따른/따라`·`지정된`이 A를 한정하는 근거 수식어이면 전체를 제거하고 A만 endpoint로 반환한다. `대통령령으로 정하는 A`, `총리령으로 정하는 A`처럼 위임 근거가 A를 수식할 때도 위임 표현을 제거하고 A만 남긴다. 단, 법령이나 조문 자체가 문장의 실제 주어·목적어이면 그 endpoint는 유지한다.
6. `사항`, `내용`, `대상`, `업무`, `행위`, `것`, `경우` 같은 일반명사는 단독 object로 반환하지 말고, 업무·행위의 종류를 식별하는 핵심 의미 수식어만 포함한다. 금액·기간·조건·비율·횟수·범위·자격 수식어는 포함하지 않는다.
7. 병렬 주어·목적어와 `및`, `또는`, `와/과`, `ㆍ`, `,`로 연결된 열거 endpoint는 결합된 문자열로 반환하지 말고 공통 predicate를 적용한 독립 SVO로 나눈다. 주어와 목적어가 모두 열거이고 모든 조합에 공통 predicate가 적용되면 각 조합을 반환한다. 병렬 predicate도 각각 완전한 행위 표현을 가진 독립 SVO로 나눈다. 예: `문서에 서명 또는 날인한다`는 `서명한다`, `날인한다` 두 관계로 분리한다. 단, 법령명·기관명·정의된 용어 등 하나의 고유명은 분리하지 않는다.
7-1. `A 또는 B`, `A 및 B`가 endpoint에 그대로 남아 있는 JSON을 반환하지 않는다. 고유명인지 확실하지 않고 열거로 읽히면 분리하는 쪽을 우선한다.
8. 수동문에서 행위자가 명시되면 반드시 의미상 행위자를 subject, 행위 대상을 object로 둔다. 예: `원장은 대통령이 임명한다` → `대통령 / 임명한다 / 원장`. 행위자가 없으면 원문의 문법상 주어와 상대 endpoint를 유지한다. 예: `신청서는 위원회에 제출된다` → `신청서 / 제출된다 / 위원회`.
9. `공시하지 않아도 된다`처럼 행위를 하지 않아도 됨을 허용하는 표현은 관계를 만들지 않는다.
10. 정의 조항도 명시적 주어·술어·목적어가 있으면 추출한다. 제목·단순 상태처럼 상대 endpoint가 없으면 만들지 않는다.
11. 병렬 행위에 공통으로 걸린 허용·부정·금지 표현은 각 predicate에 완전하게 반영한다. 예: `가입하거나 관여할 수 없다` → `가입할 수 없다`, `관여할 수 없다`. `가입하거나`처럼 연결형만 반환하지 않는다.
12. 조건·절차 표현을 endpoint나 독립 predicate로 만들지 않는다. `경우`, `의결을 거쳐`, `동의를 받아`, `제청으로`를 subject/object/predicate로 반환하지 않는다.
13. 관계가 없으면 빈 relations 배열을 반환한다. 설명, confidence, 이유, 추가 필드를 반환하지 않는다.

제약 제거·열거 분리 예시:
- `금감원은 3천만원 이하의 벌금을 부과한다` → `금감원 / 부과한다 / 벌금`
- `2년간의 사업계획서` → `사업계획서`
- `4촌 이내의 인척` → `인척`
- `500만원 이하의 과태료` → `과태료`
- `5명 이내의 대표자` → `대표자`
- `6급 이하 공무원` → `공무원`
- `건축사법에 따른 공사감리` → `공사감리`
- `「공공기관의 운영에 관한 법률」 제4조에 따라 지정된 공공기관` → `공공기관`
- `위반자를 10년 이하의 징역 또는 5억원 이하의 벌금에 처한다` → `위반자 / 처한다 / 징역`, `위반자 / 처한다 / 벌금`
- `A 또는 B는 C 또는 D를 처리한다`에서 predicate가 모든 조합에 공통이면 `A/C`, `A/D`, `B/C`, `B/D`를 별도 SVO로 반환한다.
- `「자본시장과 금융투자업에 관한 법률」`처럼 전체가 하나의 법령명이면 나누지 않는다.

말단 호 결합 예시:
- context_text: `위원회는 다음 각 호의 업무를 처리한다.`
- unit_text: `신고 접수`
- 결과: `위원회 / 처리한다 / 신고 접수`
- `위원회 / 처리한다 / 각 호의 업무`는 만들지 않는다.

반환 형식은 반드시 다음 JSON 객체다.
{"relations": [{"subject": "원문에서 제약을 제거한 핵심 엔티티 표현", "predicate": "원문 행위 표현", "object": "원문에서 제약을 제거한 핵심 엔티티 표현"}]}
"""

# Runtime source of truth. The preceding literal remains temporarily for
# source compatibility with older patches, but every caller receives the
# role-named prompt asset used by manifests and closed-network handoff.
DEFAULT_RAW_SVO_PROMPT = load_prompt_asset("raw_svo_extract.ko.md")


class CompletionClient(Protocol):
    def complete(self, prompt: str) -> str: ...


@dataclass(frozen=True)
class OpenAICompatConfig:
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float = 120.0
    max_retries: int = 2
    max_tokens: int = 1024
    enable_thinking: bool = False
    trust_env: bool = False
    local_only: bool = True

    @classmethod
    def from_env(cls) -> "OpenAICompatConfig":
        return cls(
            base_url=os.environ.get("VLLM_BASE_URL", "http://127.0.0.1:8000/v1"),
            api_key=os.environ.get("VLLM_API_KEY", "local"),
            model=os.environ.get("VLLM_MODEL", "Qwen/Qwen3.8-27B"),
            timeout_seconds=float(os.environ.get("VLLM_TIMEOUT_SECONDS", "120")),
            max_retries=int(os.environ.get("VLLM_MAX_RETRIES", "2")),
            max_tokens=int(os.environ.get("VLLM_MAX_TOKENS", "1024")),
            enable_thinking=_as_bool(os.environ.get("VLLM_ENABLE_THINKING", "false")),
            trust_env=_as_bool(os.environ.get("VLLM_TRUST_ENV", "false")),
            local_only=_as_bool(os.environ.get("SVO_LOCAL_ONLY", "true")),
        )


class OpenAICompatClient:
    """Minimal client for vLLM and other OpenAI-compatible local servers."""

    def __init__(self, config: OpenAICompatConfig):
        self._config = config
        self._validate_endpoint(config)
        self._client = httpx.Client(timeout=config.timeout_seconds, trust_env=config.trust_env)

    def close(self) -> None:
        self._client.close()

    def complete(self, prompt: str) -> str:
        payload = {
            "model": self._config.model,
            "messages": [
                {"role": "system", "content": "Return valid JSON only. Do not use Markdown fences."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "max_tokens": self._config.max_tokens,
            "response_format": {"type": "json_object"},
            "chat_template_kwargs": {"enable_thinking": self._config.enable_thinking},
        }
        last_error: Exception | None = None
        for attempt in range(self._config.max_retries + 1):
            try:
                response = self._client.post(
                    f"{self._config.base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {self._config.api_key}"},
                    json=payload,
                )
                response.raise_for_status()
                response_body = response.json()
                content = response_body["choices"][0]["message"]["content"]
                if isinstance(content, list):
                    content = "".join(
                        block.get("text", "") for block in content if isinstance(block, dict)
                    )
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("LLM response did not include text content")
                return content
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                last_error = exc
                if attempt == self._config.max_retries:
                    break
                time.sleep(0.5 * (attempt + 1))
        raise RuntimeError("OpenAI-compatible completion request failed") from last_error

    @staticmethod
    def _validate_endpoint(config: OpenAICompatConfig) -> None:
        if not config.local_only:
            return
        parsed = urlparse(config.base_url)
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError(
                "SVO_LOCAL_ONLY=true permits only loopback LLM endpoints. "
                "Set it to false only after explicitly approving a private endpoint."
            )


def extract_raw_svo(
    unit: SemanticUnit,
    client: CompletionClient,
    *,
    prompt_template: str = DEFAULT_RAW_SVO_PROMPT,
    preserve_llm_output: bool = False,
) -> list[RawRelation]:
    payload = {
        "document_title": unit.document_title,
        "source_ref": unit.source_ref.to_dict(),
        "context_text": unit.context_text,
        "governing_text": unit.governing_text,
        "unit_text": unit.unit_text,
        "resolved_text": unit.resolved_text,
        "extraction_text": unit.resolved_text or unit.unit_text,
    }
    response_text = client.complete(f"{prompt_template}\n\n입력:\n{json.dumps(payload, ensure_ascii=False)}")
    raw_relations = _parse_relations(response_text)
    relations: list[RawRelation] = []
    seen: set[tuple[str, str, str]] = set()
    for raw_relation in raw_relations:
        try:
            triple = (
                _required_text(raw_relation, "subject"),
                _required_text(raw_relation, "predicate"),
                _required_text(raw_relation, "object"),
            )
        except ValueError:
            # A malformed relation is not itself evidence. Preserve other
            # valid triples in the response; if none remain the semantic unit
            # correctly becomes an empty relations array.
            continue
        output_triples = (
            [triple]
            if preserve_llm_output
            else _sanitize_and_split_triple(triple)
        )
        for sanitized_triple in output_triples:
            if sanitized_triple in seen:
                continue
            seen.add(sanitized_triple)
            relations.append(
                RawRelation(
                    relation_mention_id=stable_id(
                        "raw_svo_mention", unit.semantic_unit_id, *sanitized_triple
                    ),
                    subject=sanitized_triple[0],
                    predicate=sanitized_triple[1],
                    object=sanitized_triple[2],
                )
            )
    return relations


def extract_units(
    units: Sequence[SemanticUnit],
    client: CompletionClient,
    *,
    workers: int = 1,
    prompt_template: str = DEFAULT_RAW_SVO_PROMPT,
    preserve_llm_output: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract in parallel while preserving the source-unit ordering in output."""

    if workers < 1:
        raise ValueError("workers must be at least 1")
    outputs: list[dict[str, Any] | None] = [None] * len(units)
    errors: list[dict[str, Any]] = []

    def process(index: int, unit: SemanticUnit) -> tuple[int, dict[str, Any]]:
        relations = extract_raw_svo(
            unit,
            client,
            prompt_template=prompt_template,
            preserve_llm_output=preserve_llm_output,
        )
        record = unit.to_dict()
        record["relations"] = [relation.to_dict() for relation in relations]
        return index, record

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(process, index, unit): (index, unit) for index, unit in enumerate(units)}
        for future in as_completed(futures):
            index, unit = futures[future]
            try:
                output_index, record = future.result()
                outputs[output_index] = record
            except Exception as exc:  # one faulty LLM response must not discard the batch
                errors.append(
                    {
                        "semantic_unit_id": unit.semantic_unit_id,
                        "document_id": unit.document_id,
                        "source_ref": unit.source_ref.to_dict(),
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
    return [record for record in outputs if record is not None], errors


def load_prompt(path: Path | None) -> str:
    if path is None:
        return DEFAULT_RAW_SVO_PROMPT
    return path.read_text(encoding="utf-8")


def _parse_relations(response_text: str) -> list[Mapping[str, Any]]:
    candidate = response_text.strip()
    if candidate.startswith("```"):
        candidate = candidate.split("\n", maxsplit=1)[-1]
        if candidate.rstrip().endswith("```"):
            candidate = candidate.rstrip()[:-3]
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError("LLM response is not valid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("relations"), list):
        raise ValueError("LLM response must be an object with a relations array")
    if not all(isinstance(relation, Mapping) for relation in payload["relations"]):
        raise ValueError("Every relation must be a JSON object")
    return payload["relations"]


def _required_text(relation: Mapping[str, Any], key: str) -> str:
    value = relation.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Relation {key} must be a non-empty string")
    return value.strip()


_GENERIC_ONLY_ENDPOINTS = {
    "각 호",
    "각호",
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
_QUANTITY_UNIT = (
    r"(?:조원|억원|만원|천원|원|년|개월|일|시간|회|명|인|세|등급|급|촌|점|통|주|부|건|개|퍼센트|%)"
)
_QUANTITY_CONDITION = (
    rf"\d[\d,]*(?:\.\d+)?\s*{_QUANTITY_UNIT}\s*"
    r"(?:이상|이하|초과|미만|이내|간|동안)?"
)
_PURE_CONDITION_RE = re.compile(
    rf"^(?:약\s*)?{_QUANTITY_CONDITION}(?:\s*(?:부터|까지))?$"
)
_LEADING_CONDITION_RE = re.compile(
    rf"^(?:약\s*)?{_QUANTITY_CONDITION}\s*(?:의\s*)?(?P<core>.+)$"
)
_INLINE_CONDITION_RE = re.compile(
    rf"\s*(?:제\s*)?{_QUANTITY_CONDITION}"
    r"(?=\s|$|의|에|이|가|을|를|은|는)"
    r"\s*(?:의\s*|(?:이|가|을|를|은|는)\s*)?"
)
_PURE_RANGE_CONDITION_RE = re.compile(
    rf"^(?:{_QUANTITY_CONDITION}\s*)?부터\s*{_QUANTITY_CONDITION}\s*까지"
    r"(?:의\s*(?:구분|범위))?$"
)
_CLEAR_ENUMERATION_RE = re.compile(
    r"\s*(?:,|ㆍ|·)\s*|\s+(?:또는|및)\s+"
)
_LEGAL_NAME_SUFFIX_RE = re.compile(r"(?:법|법률|시행령|시행규칙|규정)$")
_EXPLICIT_ENUMERATION_RE = re.compile(r",|ㆍ|·|\s+(?:또는|및)\s+")
_QUALIFIED_PERSON_RE = re.compile(
    r"(?:경력이\s*있는|자격이\s*있는|재직한|근무한|담당한|종사한).*(?:사람|자)$"
)
_PERIOD_QUALIFIED_PERSON_RE = re.compile(
    r"\d[\d,]*(?:\.\d+)?\s*(?:년|개월).*?지나지\s*(?:아니한|않은).*(?:사람|자)$"
)
_SHARED_GENITIVE_ENUM_RE = re.compile(r"^(?P<prefix>.+의)\s+(?P<head>\S+)$")


def _sanitize_and_split_triple(
    triple: tuple[str, str, str],
) -> list[tuple[str, str, str]]:
    subject, predicate, object_ = triple
    subjects = _sanitize_endpoint(subject)
    objects = _sanitize_endpoint(object_)
    if not subjects or not objects:
        return []
    return [
        (sanitized_subject, predicate, sanitized_object)
        for sanitized_subject in subjects
        for sanitized_object in objects
    ]


def _sanitize_endpoint(value: str) -> list[str]:
    value = value.strip()
    if _is_discardable_endpoint(value):
        return []
    value = _strip_leading_condition(value)
    if _is_discardable_endpoint(value):
        return []
    if _PERIOD_QUALIFIED_PERSON_RE.search(value):
        return [value]
    if _QUALIFIED_PERSON_RE.search(value):
        return ["사람"]
    if _is_protected_named_expression(value):
        return [value]
    if _PURE_RANGE_CONDITION_RE.fullmatch(value):
        return []
    value = re.sub(r"\s+", " ", _INLINE_CONDITION_RE.sub(" ", value)).strip()
    value = re.sub(r"도\s+에\s+", "도 ", value)
    value = re.sub(r"\s+(?=(?:에|에서|으로|로)\s)", "", value)
    value = re.sub(r"^(?:및|또는)\s+|\s+(?:및|또는)$", "", value).strip()
    if _is_discardable_endpoint(value):
        return []
    parts = _split_enumeration_with_shared_modifier(value)
    if len(parts) > 1:
        sanitized_parts = []
        for part in parts:
            part = _strip_leading_condition(part)
            if not _is_discardable_endpoint(part):
                sanitized_parts.append(part)
        return list(dict.fromkeys(sanitized_parts))
    return [value]


def _split_enumeration_with_shared_modifier(value: str) -> list[str]:
    parts = [part.strip() for part in _CLEAR_ENUMERATION_RE.split(value)]
    if len(parts) <= 1:
        return parts
    match = _SHARED_GENITIVE_ENUM_RE.fullmatch(parts[0])
    if not match:
        return parts
    prefix = match.group("prefix")
    return [
        part
        if index == 0 or "의 " in part or part.startswith(prefix)
        else f"{prefix} {part}"
        for index, part in enumerate(parts)
    ]


def sanitize_raw_svo_records(
    records: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Reapply deterministic endpoint rules to stored raw-SVO records.

    This supports long runs that were already in progress when a deterministic
    sanitizer rule was tightened. Source fields and raw evidence are retained;
    relation mention IDs are regenerated from the final triples.
    """

    output: list[dict[str, Any]] = []
    for source_record in records:
        record = dict(source_record)
        relations: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for relation in source_record.get("relations", []):
            triple = (
                _required_text(relation, "subject"),
                _required_text(relation, "predicate"),
                _required_text(relation, "object"),
            )
            for sanitized in _sanitize_and_split_triple(triple):
                if sanitized in seen:
                    continue
                seen.add(sanitized)
                relations.append(
                    {
                        "relation_mention_id": stable_id(
                            "raw_svo_mention",
                            record["semantic_unit_id"],
                            *sanitized,
                        ),
                        "subject": sanitized[0],
                        "predicate": sanitized[1],
                        "object": sanitized[2],
                    }
                )
        record["relations"] = relations
        output.append(record)
    return output


def align_raw_svo_records(
    unit_ids: Sequence[str],
    records: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Validate exact coverage and restore source semantic-unit order."""

    records_by_id: dict[str, dict[str, Any]] = {}
    for record in records:
        semantic_unit_id = str(record.get("semantic_unit_id", ""))
        if not semantic_unit_id:
            raise ValueError("raw SVO record has no semantic_unit_id")
        if semantic_unit_id in records_by_id:
            raise ValueError(f"duplicate raw SVO record: {semantic_unit_id}")
        records_by_id[semantic_unit_id] = record
    expected = list(unit_ids)
    if len(expected) != len(set(expected)):
        raise ValueError("semantic unit IDs are not unique")
    missing = [unit_id for unit_id in expected if unit_id not in records_by_id]
    extra = sorted(set(records_by_id) - set(expected))
    if missing or extra:
        raise ValueError(
            f"raw SVO coverage mismatch: missing={len(missing)}, extra={len(extra)}"
        )
    return [records_by_id[unit_id] for unit_id in expected]


def _strip_leading_condition(value: str) -> str:
    match = _LEADING_CONDITION_RE.match(value)
    return match.group("core").strip() if match else value.strip()


def _is_discardable_endpoint(value: str) -> bool:
    return bool(
        not value
        or value in _GENERIC_ONLY_ENDPOINTS
        or _PURE_CONDITION_RE.fullmatch(value)
    )


def _is_protected_named_expression(value: str) -> bool:
    is_fully_quoted = (
        (value.startswith("「") and value.endswith("」"))
        or (value.startswith("『") and value.endswith("』"))
    )
    if is_fully_quoted:
        return True
    return bool(
        _LEGAL_NAME_SUFFIX_RE.search(value)
        and not _EXPLICIT_ENUMERATION_RE.search(value)
    )


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}
