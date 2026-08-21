"""Grounded LLM answer generation for already-ranked graph paths."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Protocol, Sequence

from .models import AnswerCitation, AnswerMetadata, GraphPath


_CITATION_RE = re.compile(r"\[(E[1-9][0-9]*)\]")
_MAX_ANSWER_CHARACTERS = 6000


class AnswerCompletionClient(Protocol):
    def complete(self, prompt: str) -> str: ...


@dataclass(frozen=True)
class GroundedAnswerConfig:
    prompt_path: Path
    max_paths: int = 5
    max_evidence_items: int = 15
    timeout_seconds: float = 45.0

    def validate(self) -> None:
        if not self.prompt_path.is_file():
            raise FileNotFoundError(f"answer prompt does not exist: {self.prompt_path}")
        if self.max_paths < 1:
            raise ValueError("answer.max_paths must be at least 1")
        if self.max_evidence_items < 1:
            raise ValueError("answer.max_evidence_items must be at least 1")
        if not 1 <= self.timeout_seconds <= 60:
            raise ValueError("answer.timeout_seconds must be between 1 and 60")


class GroundedAnswerGenerator:
    """Ask a local LLM to answer from validated path evidence only."""

    def __init__(
        self,
        client: AnswerCompletionClient,
        config: GroundedAnswerConfig,
        *,
        model: str,
        clock=monotonic,
    ) -> None:
        config.validate()
        self._client = client
        self._config = config
        self._model = model
        self._clock = clock
        self._prompt = config.prompt_path.read_text(encoding="utf-8").strip()
        if not self._prompt:
            raise ValueError("answer prompt must not be empty")
        self._prompt_name = config.prompt_path.name
        self._prompt_sha256 = hashlib.sha256(
            self._prompt.encode("utf-8")
        ).hexdigest()

    def generate(
        self,
        query: str,
        paths: Sequence[GraphPath],
        *,
        fallback_answer: str,
        deadline: float,
    ) -> tuple[str, AnswerMetadata]:
        evidence, citation_map = _collect_grounded_evidence(
            paths,
            max_paths=self._config.max_paths,
            max_evidence_items=self._config.max_evidence_items,
        )
        if not evidence:
            return fallback_answer, self._fallback(citation_map, "no_grounded_evidence")
        if deadline - self._clock() < self._config.timeout_seconds:
            return fallback_answer, self._fallback(citation_map, "insufficient_time_budget")

        payload = {"question": query, "evidence": evidence}
        prompt = f"{self._prompt}\n\n입력:\n{json.dumps(payload, ensure_ascii=False)}"
        try:
            answer, cited_keys = _parse_llm_answer(
                self._client.complete(prompt),
                allowed_keys={item.key for item in citation_map},
            )
        except Exception as exc:  # Safe deterministic result remains available.
            reason = "invalid_llm_response" if isinstance(exc, ValueError) else "llm_request_failed"
            return fallback_answer, self._fallback(citation_map, reason)
        if self._clock() >= deadline:
            return fallback_answer, self._fallback(citation_map, "answer_time_budget_exceeded")

        return answer, AnswerMetadata(
            mode="llm",
            status="generated",
            model=self._model,
            prompt_name=self._prompt_name,
            prompt_sha256=self._prompt_sha256,
            generated_at=datetime.now(UTC).isoformat(),
            citation_map=citation_map,
            cited_keys=cited_keys,
        )

    def _fallback(
        self,
        citation_map: tuple[AnswerCitation, ...],
        reason: str,
    ) -> AnswerMetadata:
        return AnswerMetadata(
            mode="llm",
            status="fallback",
            model=self._model,
            prompt_name=self._prompt_name,
            prompt_sha256=self._prompt_sha256,
            generated_at=datetime.now(UTC).isoformat(),
            citation_map=citation_map,
            fallback_reason=reason,
        )


def _collect_grounded_evidence(
    paths: Sequence[GraphPath],
    *,
    max_paths: int,
    max_evidence_items: int,
) -> tuple[list[dict[str, object]], tuple[AnswerCitation, ...]]:
    records: list[dict[str, object]] = []
    citations: list[AnswerCitation] = []
    seen_mentions: set[str] = set()
    for path_number, path in enumerate(paths[:max_paths], start=1):
        for edge in path.edges:
            for item in edge.evidences:
                if item.relation_mention_id in seen_mentions:
                    continue
                seen_mentions.add(item.relation_mention_id)
                key = f"E{len(citations) + 1}"
                citations.append(AnswerCitation(key, item.relation_mention_id))
                records.append(
                    {
                        "citation_key": key,
                        "path_number": path_number,
                        "relation": {
                            "source": edge.source_name,
                            "predicate": edge.relation_type_name,
                            "target": edge.target_name,
                            "polarity": edge.polarity,
                        },
                        "source": {
                            "document_title": item.document_title,
                            "source_ref": item.source_ref,
                            "unit_kind": item.unit_kind,
                            "context_text": item.context_text,
                            "unit_text": item.unit_text,
                            "raw_subject": item.raw_subject,
                            "raw_predicate": item.raw_predicate,
                            "raw_object": item.raw_object,
                        },
                    }
                )
                if len(records) >= max_evidence_items:
                    return records, tuple(citations)
    return records, tuple(citations)


def _parse_llm_answer(
    response_text: str,
    *,
    allowed_keys: set[str],
) -> tuple[str, tuple[str, ...]]:
    try:
        payload = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise ValueError("LLM answer must be a JSON object") from exc
    if not isinstance(payload, dict):
        raise ValueError("LLM answer must be a JSON object")
    answer = payload.get("answer")
    if not isinstance(answer, str):
        raise ValueError("LLM answer must contain a string answer field")
    answer = answer.strip()
    if not answer or len(answer) > _MAX_ANSWER_CHARACTERS:
        raise ValueError("LLM answer is empty or too long")
    cited_keys = tuple(dict.fromkeys(_CITATION_RE.findall(answer)))
    if not cited_keys:
        raise ValueError("LLM answer must cite at least one evidence key")
    if any(key not in allowed_keys for key in cited_keys):
        raise ValueError("LLM answer cited evidence outside the supplied context")
    return answer, cited_keys
