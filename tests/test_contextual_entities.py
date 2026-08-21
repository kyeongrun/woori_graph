import json

from woori_graph.contextual_entities import (
    audit_contextual_entity_dictionary,
    build_contextually_normalized_entity_dictionary,
    entity_lexical_key,
    is_acceptable_llm_canonical_mapping,
    needs_contextual_entity_normalization,
    propose_contextual_entity_mapping,
    rekey_llm_mapping_by_source_name,
    sanitize_contextual_entity_mapping,
)


class _FakeClient:
    def complete(self, prompt: str) -> str:
        return json.dumps(
            {
                "items": [
                    {"source_entity_id": "entity-1", "canonical_name": "사업계획서"}
                ]
            },
            ensure_ascii=False,
        )


class _FailingClient:
    def complete(self, prompt: str) -> str:
        raise RuntimeError("test failure")


def test_contextual_entity_second_pass_preserves_mentions():
    source = [
        {
            "entity_id": "entity-1",
            "canonical_name": "2년간의 사업계획서",
            "scope": {"type": "global"},
            "aliases": [
                {
                    "name": "2년간의 사업계획서",
                    "scope": {"type": "global"},
                    "mention_count": 2,
                    "sample_source_refs": [],
                    "is_canonical": True,
                }
            ],
            "sample_source_refs": [],
            "mention_count": 2,
            "normalization_methods": [],
            "review_status": "first_pass",
        }
    ]
    raw = [
        {
            "document_title": "테스트법",
            "source_ref": {"article": "제1조", "paragraph": 1, "item_path": []},
            "context_text": "회사는 사업계획서를 제출한다.",
            "unit_text": "2년간의 사업계획서",
            "relations": [
                {
                    "subject": "회사",
                    "predicate": "제출한다",
                    "object": "2년간의 사업계획서",
                }
            ],
        }
    ]

    mapping, errors = propose_contextual_entity_mapping(
        source, raw, _FakeClient(), batch_size=1, workers=1
    )
    output = build_contextually_normalized_entity_dictionary(source, mapping)
    audit = audit_contextual_entity_dictionary(source, mapping, output)

    assert not errors
    assert output[0]["canonical_name"] == "사업계획서"
    assert output[0]["mention_count"] == 2
    assert audit["passed"] is True


def test_strict_llm_mapping_never_creates_fallback_names():
    source = [
        {
            "entity_id": "entity-1",
            "canonical_name": "원표현",
            "aliases": [],
            "sample_source_refs": [],
            "mention_count": 1,
        }
    ]

    mapping, errors = propose_contextual_entity_mapping(
        source,
        [],
        _FailingClient(),
        batch_size=1,
        workers=1,
        require_all_llm=True,
    )

    assert mapping == []
    assert len(errors) == 1


def test_strict_audit_requires_llm_canonical_name_for_every_entity():
    source = [{"entity_id": "entity-1", "canonical_name": "원표현", "mention_count": 1}]
    mapping = [
        {
            "source_entity_id": "entity-1",
            "source_canonical_name": "원표현",
            "canonical_name": "원표현",
            "normalization_status": "identity_not_selected_for_contextual_pass",
        }
    ]
    output = [{"entity_id": "entity-1", "canonical_name": "원표현", "mention_count": 1}]

    audit = audit_contextual_entity_dictionary(
        source, mapping, output, require_all_llm=True
    )

    assert audit["passed"] is False
    assert audit["checks"]["all_canonical_names_selected_by_llm"] is False


def test_contextual_candidate_selection_covers_conditions_and_spacing_duplicates():
    assert needs_contextual_entity_normalization("2년간의 사업계획서") is True
    assert needs_contextual_entity_normalization("금융위원회") is False
    assert entity_lexical_key("감사 결과") == entity_lexical_key("감사결과")


def test_contextual_mapping_rejects_generic_only_reduction():
    mapping = sanitize_contextual_entity_mapping(
        [
            {
                "source_entity_id": "entity-1",
                "source_canonical_name": "대통령령으로 정하는 자",
                "canonical_name": "자",
                "normalization_status": "llm_contextual_second_pass",
            }
        ]
    )

    assert mapping[0]["canonical_name"] == "대통령령으로 정하는 자"
    assert mapping[0]["normalization_status"] == "guarded_generic_only_result"


def test_strict_llm_validation_retries_generic_only_reduction():
    assert (
        is_acceptable_llm_canonical_mapping(
            {
                "source_canonical_name": "퇴직 후 2년이 지나지 않은 사람",
                "canonical_name": "사람",
            }
        )
        is False
    )


def test_rekey_llm_mapping_reuses_decision_for_identical_source_name():
    current = [{"entity_id": "new-id", "canonical_name": "금융위원회"}]
    prior = [
        {
            "source_entity_id": "old-id",
            "source_canonical_name": "금융위원회",
            "canonical_name": "금융위원회",
            "normalization_status": "llm_canonical_name",
        }
    ]

    mapping = rekey_llm_mapping_by_source_name(current, prior)

    assert mapping == [
        {
            "source_entity_id": "new-id",
            "source_canonical_name": "금융위원회",
            "canonical_name": "금융위원회",
            "normalization_status": "llm_canonical_name_rekeyed",
        }
    ]


def test_contextual_mapping_keeps_qualified_endpoint_instead_of_law_citation():
    mapping = sanitize_contextual_entity_mapping(
        [
            {
                "source_entity_id": "entity-1",
                "source_canonical_name": "공공기관의 운영에 관한 법률 제4조에 따라 지정된 공공기관",
                "canonical_name": "공공기관의 운영에 관한 법률 제4조",
                "normalization_status": "llm_contextual_second_pass",
            }
        ]
    )

    assert mapping[0]["canonical_name"] == "공공기관"
    assert mapping[0]["normalization_status"] == "rule_qualified_endpoint_not_law"


def test_contextual_mapping_converts_bare_section_to_law_name():
    mapping = sanitize_contextual_entity_mapping(
        [
            {
                "source_entity_id": "entity-1",
                "source_canonical_name": "법 제115조제2항",
                "canonical_name": "보험업법 제115조제2항",
                "normalization_status": "llm_contextual_second_pass",
            }
        ]
    )

    assert mapping[0]["canonical_name"] == "보험업법"
    assert mapping[0]["normalization_status"] == "rule_bare_citation_to_law_name"
