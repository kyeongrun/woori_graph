import json

import pytest

from woori_graph.entity_typing import (
    EntityTypeValidationError,
    apply_entity_type_mapping,
    audit_entity_types,
    build_entity_type_mapping,
    infer_entity_type,
    merge_entity_type_mappings,
    propose_llm_entity_types,
    validate_typed_entity_dictionary,
)
from woori_graph.ids import stable_id


class FakeCompletionClient:
    def complete(self, prompt: str) -> str:
        payload = json.loads(prompt.split("입력:\n", maxsplit=1)[1])
        return json.dumps(
            {
                "entities": [
                    {
                        "entity_id": item["entity_id"],
                        "entity_type": "CONCEPT",
                    }
                    for item in payload["entities"]
                ]
            },
            ensure_ascii=False,
        )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("금융위원회", "ORGANIZATION"),
        ("감사위원", "PERSON"),
        ("감사", "CONCEPT"),
        ("은행법", "LEGAL_INSTRUMENT"),
        ("해석하기 어려운 대상", "OTHER"),
    ],
)
def test_infer_entity_type_uses_five_type_taxonomy(name, expected) -> None:
    assert infer_entity_type(name).entity_type == expected


def test_alias_can_supply_high_confidence_type() -> None:
    decision = infer_entity_type("금감원", [{"name": "금융감독원"}])

    assert decision.entity_type == "ORGANIZATION"
    assert decision.method == "alias_rule"


def test_mapping_preserves_identity_and_adds_type() -> None:
    entity_id = stable_id("entity", "금융위원회")
    records = [
        {
            "canonical_name": "금융위원회",
            "aliases": [{"name": "금융위원회"}],
            "entity_id": entity_id,
        }
    ]

    mapping = build_entity_type_mapping(records)
    typed = apply_entity_type_mapping(records, mapping)

    assert typed[0]["entity_id"] == entity_id
    assert typed[0]["canonical_name"] == "금융위원회"
    assert typed[0]["entity_type"] == "ORGANIZATION"
    assert audit_entity_types(records, mapping)["passed"] is True


def test_released_dictionary_requires_explicit_valid_type() -> None:
    with pytest.raises(EntityTypeValidationError, match="classify-entity-types"):
        validate_typed_entity_dictionary(
            [{"entity_id": "id-1", "canonical_name": "금융위원회"}]
        )


def test_llm_mapping_replaces_only_ambiguous_other() -> None:
    records = [
        {
            "entity_id": stable_id("entity", "해석하기 어려운 대상"),
            "canonical_name": "해석하기 어려운 대상",
            "aliases": [{"name": "해석하기 어려운 대상"}],
        }
    ]
    deterministic = build_entity_type_mapping(records)

    llm_mapping, errors = propose_llm_entity_types(
        records,
        FakeCompletionClient(),
        prompt_template="분류",
        batch_size=1,
        workers=1,
    )
    merged = merge_entity_type_mappings(deterministic, llm_mapping)

    assert errors == []
    assert merged[0]["entity_type"] == "CONCEPT"
    assert merged[0]["assignment_method"] == "llm"


def test_strict_type_audit_requires_every_mapping_to_be_llm() -> None:
    records = [
        {
            "entity_id": stable_id("entity", "금융위원회"),
            "canonical_name": "금융위원회",
            "aliases": [{"name": "금융위원회"}],
        }
    ]
    deterministic = build_entity_type_mapping(records)

    audit = audit_entity_types(records, deterministic, require_all_llm=True)

    assert audit["passed"] is False
    assert audit["checks"]["all_entity_types_assigned_by_llm"] is False


def test_llm_type_progress_reports_completed_batches() -> None:
    records = [
        {
            "entity_id": stable_id("entity", "금융위원회"),
            "canonical_name": "금융위원회",
            "aliases": [{"name": "금융위원회"}],
        }
    ]
    progress = []

    mapping, errors = propose_llm_entity_types(
        records,
        FakeCompletionClient(),
        prompt_template="분류",
        batch_size=1,
        workers=1,
        progress_callback=lambda mapped, failed: progress.append((mapped, failed)),
    )

    assert errors == []
    assert mapping[0]["assignment_method"] == "llm"
    assert len(progress) == 1
    assert progress[0][1] == []
