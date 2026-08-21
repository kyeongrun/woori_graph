import json

from woori_graph.closed_relations import (
    audit_closed_relation_dictionary,
    build_closed_relation_dictionary,
    propose_closed_relation_mapping,
    propose_relation_taxonomy,
    sanitize_closed_relation_mapping,
)


class _FakeClient:
    def __init__(self, responses):
        self.responses = iter(responses)

    def complete(self, prompt: str) -> str:
        return next(self.responses)


def _relations():
    return [
        {
            "relation_type_id": "source-1",
            "canonical_name": "제출하다",
            "polarity": "POSITIVE",
            "mention_count": 3,
            "aliases": [
                {"name": "제출한다", "mention_count": 3, "sample_source_refs": []}
            ],
        },
        {
            "relation_type_id": "source-2",
            "canonical_name": "송부하지않다",
            "polarity": "NEGATIVE",
            "mention_count": 2,
            "aliases": [
                {
                    "name": "송부하여서는 아니 된다",
                    "mention_count": 2,
                    "sample_source_refs": [],
                }
            ],
        },
    ]


def test_closed_relation_taxonomy_mapping_and_audit():
    client = _FakeClient(
        [
            json.dumps(
                {
                    "items": [
                        {
                            "positive_name": "전달하다",
                            "negative_name": "전달하지않다",
                            "description": "자료나 정보를 상대방에게 전달하는 행위",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "items": [
                        {"source_canonical_name": "제출하다", "family_id": "R001"},
                        {
                            "source_canonical_name": "송부하지않다",
                            "family_id": "R001",
                        },
                    ]
                },
                ensure_ascii=False,
            ),
        ]
    )
    source = _relations()
    taxonomy = propose_relation_taxonomy(source, client, target_families=1)
    mapping, errors = propose_closed_relation_mapping(
        source, taxonomy, client, batch_size=2, workers=1
    )
    output = build_closed_relation_dictionary(source, mapping)
    audit = audit_closed_relation_dictionary(source, mapping, output)

    assert not errors
    assert [item["canonical_name"] for item in output] == ["전달하다", "전달하지않다"]
    assert audit["passed"] is True
    assert audit["counts"]["output_relation_types"] == 2


def test_closed_relation_semantic_guard_avoids_opposite_direction_family():
    taxonomy = [
        {
            "family_id": "R003",
            "positive_name": "승인하다",
            "negative_name": "승인하지않다",
            "description": "승인",
        },
        {
            "family_id": "R031",
            "positive_name": "제한하다",
            "negative_name": "제한하지않다",
            "description": "제한",
        },
    ]
    mapping = sanitize_closed_relation_mapping(
        [
            {
                "source_relation_type_id": "source-1",
                "source_canonical_name": "거절하다",
                "source_polarity": "POSITIVE",
                "target_family_id": "R003",
                "target_canonical_name": "승인하다",
                "mention_count": 1,
                "mapping_status": "llm_closed_taxonomy",
            }
        ],
        taxonomy,
    )

    assert mapping[0]["target_family_id"] == "R031"
    assert mapping[0]["target_canonical_name"] == "제한하다"
    assert mapping[0]["mapping_status"] == "rule_semantic_direction_guard"


def test_closed_relation_semantic_guard_does_not_rewrite_compound_action():
    taxonomy = [
        {
            "family_id": "R039",
            "positive_name": "등록하다",
            "negative_name": "등록하지않다",
            "description": "등록",
        },
        {
            "family_id": "R045",
            "positive_name": "종료하다",
            "negative_name": "종료하지않다",
            "description": "종료",
        },
    ]
    original = {
        "source_relation_type_id": "source-1",
        "source_canonical_name": "종료등기하다",
        "source_polarity": "POSITIVE",
        "target_family_id": "R045",
        "target_canonical_name": "종료하다",
        "mention_count": 1,
        "mapping_status": "llm_closed_taxonomy",
    }

    mapping = sanitize_closed_relation_mapping([original], taxonomy)

    assert mapping[0]["target_family_id"] == "R039"
    assert mapping[0]["target_canonical_name"] == "등록하다"
