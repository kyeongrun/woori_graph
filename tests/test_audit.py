from woori_graph.audit import audit_artifacts, audit_raw_svo
from woori_graph.ids import stable_id


def test_audit_confirms_cross_artifact_ids() -> None:
    entity_a = "550e8400-e29b-41d4-a716-446655440000"
    entity_b = "550e8400-e29b-41d4-a716-446655440001"
    relation_type = "550e8400-e29b-41d4-a716-446655440002"
    relation = "550e8400-e29b-41d4-a716-446655440003"
    units = [{"semantic_unit_id": "unit-1"}]
    raw = [
        {
            "semantic_unit_id": "unit-1",
            "relations": [{"relation_mention_id": "mention-1"}],
        }
    ]
    entities = [{"entity_id": entity_a}, {"entity_id": entity_b}]
    relation_types = [{"relation_type_id": relation_type}]
    edges = [
        {
            "relation_id": relation,
            "source_entity_id": entity_a,
            "target_entity_id": entity_b,
            "relation_type_id": relation_type,
            "evidence_count": 1,
            "evidence": [{"relation_mention_id": "mention-1"}],
        }
    ]

    report = audit_artifacts(units, raw, entities, relation_types, edges)

    assert all(report["checks"].values())


def test_audit_rejects_duplicate_dictionary_ids() -> None:
    duplicated = "550e8400-e29b-41d4-a716-446655440000"

    report = audit_artifacts(
        [],
        [],
        [{"entity_id": duplicated}, {"entity_id": duplicated}],
        [{"relation_type_id": duplicated}, {"relation_type_id": duplicated}],
        [],
    )

    assert report["checks"]["entity_ids_unique"] is False
    assert report["checks"]["relation_type_ids_unique"] is False


def test_raw_svo_audit_checks_coverage_order_and_stable_mentions() -> None:
    unit = {
        "semantic_unit_id": "f4dd686a-41c3-5aa5-b8f2-548effb165ac",
        "document_id": "b0622b0e-92d8-5c3b-919d-e9801104404a",
        "document_title": "테스트법",
        "source_path": "테스트법/법률.md",
        "source_ref": {"article": "제1조", "paragraph": 1, "item_path": []},
        "context_text": "##### 제1조",
        "unit_text": "위원회는 벌금을 부과한다.",
        "unit_kind": "paragraph",
    }
    triple = ("위원회", "부과한다", "벌금")
    raw = {
        **unit,
        "relations": [
            {
                "relation_mention_id": stable_id("raw_svo_mention", unit["semantic_unit_id"], *triple),
                "subject": triple[0],
                "predicate": triple[1],
                "object": triple[2],
            }
        ],
    }

    report = audit_raw_svo([unit], [raw])

    assert report["passed"] is True
    assert report["counts"]["raw_relations"] == 1
    assert report["quality_warnings"]["numeric_constraint_endpoint_samples"] == []
