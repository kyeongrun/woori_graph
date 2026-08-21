import json

import pytest

from woori_graph.context_resolution import (
    audit_context_resolution,
    normalize_context_resolution_records,
    resolve_unit_context,
    resolve_units,
)
from woori_graph.models import SemanticUnit, SourceRef


class FakeClient:
    def __init__(self, responses: list[dict[str, str] | str]) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        response = self.responses.pop(0)
        return response if isinstance(response, str) else json.dumps(response, ensure_ascii=False)


def _unit(identifier: str = "unit-1") -> SemanticUnit:
    return SemanticUnit(
        semantic_unit_id=identifier,
        document_id="doc-1",
        document_title="감사원법",
        source_path="감사원법/법률.md",
        source_ref=SourceRef(article="제12조", paragraph=1, item_path=("1",)),
        context_text="제12조 제1항\n다음 각 호의 사항은 감사위원회의에서 결정한다.",
        unit_text="감사원의 감사정책 및 주요 감사계획에 관한 사항",
        unit_kind="terminal_item",
        governing_text="다음 각 호의 사항은 감사위원회의에서 결정한다.",
    )


def test_resolves_terminal_item_without_overwriting_source_text() -> None:
    client = FakeClient(
        [
            {
                "resolved_text": "감사위원회는 감사원의 감사정책 및 주요 감사계획에 관한 사항을 결정한다.",
                "resolution_type": "CONTEXT_INHERITED",
            }
        ]
    )

    resolved = resolve_unit_context(_unit(), client)

    assert resolved.unit_text == "감사원의 감사정책 및 주요 감사계획에 관한 사항"
    assert resolved.governing_text == "다음 각 호의 사항은 감사위원회의에서 결정한다."
    assert resolved.resolved_text.startswith("감사위원회는")
    assert resolved.resolution_type == "CONTEXT_INHERITED"
    assert '"governing_text"' in client.prompts[0]


def test_resolution_preserves_input_order_with_parallel_workers() -> None:
    client = FakeClient(
        [
            {"resolved_text": "첫 문장.", "resolution_type": "COPIED"},
            {"resolved_text": "둘째 문장.", "resolution_type": "COPIED"},
        ]
    )

    records, errors = resolve_units([_unit("unit-1"), _unit("unit-2")], client, workers=1)

    assert [record["semantic_unit_id"] for record in records] == ["unit-1", "unit-2"]
    assert errors == []


def test_complete_paragraph_is_copied_without_llm_call() -> None:
    unit = SemanticUnit(
        semantic_unit_id="paragraph-1",
        document_id="doc-1",
        document_title="테스트법",
        source_path="테스트법/법률.md",
        source_ref=SourceRef(article="제1조", paragraph=None, item_path=()),
        context_text="제1조",
        unit_text="위원회는 보고서를 제출한다.",
        unit_kind="paragraph",
    )
    client = FakeClient([])

    resolved = resolve_unit_context(unit, client)

    assert resolved.resolved_text == unit.unit_text
    assert resolved.resolution_type == "COPIED"
    assert client.prompts == []


def test_rejects_resolution_without_allowed_type() -> None:
    client = FakeClient([{"resolved_text": "문장.", "resolution_type": "UNKNOWN"}])

    with pytest.raises(ValueError, match="COPIED or CONTEXT_INHERITED"):
        resolve_unit_context(_unit(), client)


def test_context_resolution_audit_requires_exact_coverage_and_source_preservation() -> None:
    source = _unit().to_dict()
    resolved = {
        **source,
        "resolved_text": "감사위원회는 감사원의 감사정책 및 주요 감사계획에 관한 사항을 결정한다.",
        "resolution_type": "CONTEXT_INHERITED",
    }

    report = audit_context_resolution([source], [resolved])

    assert report["passed"] is True
    assert report["checks"]["source_fields_preserved"] is True
    assert report["counts"]["resolution_types"]["CONTEXT_INHERITED"] == 1


def test_context_resolution_audit_rejects_non_verbatim_copied_text() -> None:
    source = _unit().to_dict()
    resolved = {
        **source,
        "resolved_text": "다르게 쓴 문장.",
        "resolution_type": "COPIED",
    }

    report = audit_context_resolution([source], [resolved])

    assert report["passed"] is False
    assert report["checks"]["copied_units_are_verbatim"] is False


def test_normalize_context_resolution_derives_type_from_text_equality() -> None:
    source = _unit().to_dict()
    changed = {
        **source,
        "resolved_text": "감사위원회는 해당 사항을 결정한다.",
        "resolution_type": "COPIED",
    }

    normalized = normalize_context_resolution_records([changed])

    assert normalized[0]["resolution_type"] == "CONTEXT_INHERITED"
