from woori_graph.candidates import (
    audit_simple_surface_lists,
    build_candidate_dictionaries,
    build_simple_surface_lists,
)


def test_candidates_group_only_exact_surface_forms() -> None:
    records = [
        {
            "semantic_unit_id": "unit-1",
            "document_id": "doc-1",
            "source_ref": {"article": "제1조", "paragraph": 1, "item_path": []},
            "relations": [
                {"subject": "위원회", "predicate": "제출한다", "object": "보고서"},
                {"subject": "위원회", "predicate": "제출한다", "object": "자료"},
            ],
        }
    ]

    entities, relations = build_candidate_dictionaries(records)

    assert [entity["canonical_name"] for entity in entities] == ["보고서", "위원회", "자료"]
    assert entities[1]["mention_count"] == 2
    assert relations[0]["canonical_name"] == "제출한다"
    assert relations[0]["polarity"] is None


def test_simple_surface_lists_include_source_text_and_merge_exact_duplicates() -> None:
    records = [
        {
            "semantic_unit_id": "unit-1",
            "unit_text": "위원회는 보고서를 제출한다.",
            "resolved_text": "이 법에 따른 위원회는 보고서를 제출한다.",
            "document_title": "테스트법",
            "source_ref": {"article": "제1조", "paragraph": 1, "item_path": []},
            "relations": [
                {"subject": "위원회", "predicate": "제출한다", "object": "보고서"},
                {"subject": "보고서", "predicate": "제출한다", "object": "위원회"},
            ],
        }
    ]

    entities, relations = build_simple_surface_lists(records)

    assert entities == [
        {
            "name": "보고서",
            "source_text": "이 법에 따른 위원회는 보고서를 제출한다.",
            "semantic_unit_id": "unit-1",
            "document_title": "테스트법",
            "source_ref": {"article": "제1조", "paragraph": 1, "item_path": []},
            "roles": ["object", "subject"],
            "mention_count": 2,
        },
        {
            "name": "위원회",
            "source_text": "이 법에 따른 위원회는 보고서를 제출한다.",
            "semantic_unit_id": "unit-1",
            "document_title": "테스트법",
            "source_ref": {"article": "제1조", "paragraph": 1, "item_path": []},
            "roles": ["subject", "object"],
            "mention_count": 2,
        },
    ]
    assert relations == [
        {
            "name": "제출한다",
            "source_text": "이 법에 따른 위원회는 보고서를 제출한다.",
            "semantic_unit_id": "unit-1",
            "document_title": "테스트법",
            "source_ref": {"article": "제1조", "paragraph": 1, "item_path": []},
            "mention_count": 2,
        }
    ]

    audit = audit_simple_surface_lists(records, entities, relations)
    assert audit["passed"] is True
    assert audit["counts"]["entity_candidates"] == 2
