import pytest

from woori_graph.graph_mapping import (
    DictionaryConflictError,
    UnmappedRelationError,
    build_entity_alias_index,
    map_raw_svo_to_graph,
)
from woori_graph.ids import stable_id


def _raw_record(*relations: dict) -> dict:
    return {
        "semantic_unit_id": "unit-1",
        "document_id": "doc-1",
        "document_title": "테스트법",
        "source_path": "laws/test.md",
        "source_ref": {"article": "제1조", "paragraph": 1, "item_path": []},
        "relations": list(relations),
    }


def _entity_dictionary() -> list[dict]:
    return [
        {
            "canonical_name": "금융감독원",
            "aliases": [{"name": "금감원"}, {"name": "금융감독원"}],
            "entity_id": stable_id("entity", "금융감독원"),
        }
    ]


def _relation_dictionary() -> list[dict]:
    return [
        {
            "canonical_name": "부과하다",
            "aliases": [{"name": "부과한다"}, {"name": "부과하다"}],
            "relation_type_id": stable_id("relation_type", "부과하다"),
            "polarity": "POSITIVE",
        }
    ]


def test_graph_mapping_maps_entity_alias_and_loads_unknown_name_unchanged() -> None:
    raw = _raw_record(
        {
            "relation_mention_id": "mention-1",
            "subject": "금감원",
            "predicate": "부과한다",
            "object": "벌금",
        }
    )

    bundle = map_raw_svo_to_graph(
        [raw], _entity_dictionary(), _relation_dictionary(), dictionary_version="v1"
    )

    assert {entity["canonical_name"] for entity in bundle.entities} == {
        "금융감독원",
        "벌금",
    }
    supervisor = next(
        entity for entity in bundle.entities if entity["canonical_name"] == "금융감독원"
    )
    assert supervisor["entity_id"] == stable_id("entity", "금융감독원")
    assert [alias["name"] for alias in supervisor["aliases"]] == [
        "금융감독원",
        "금감원",
    ]
    penalty = next(entity for entity in bundle.entities if entity["canonical_name"] == "벌금")
    assert penalty["entity_id"] == stable_id("entity", "벌금")
    assert penalty["dictionary_match"] is False
    assert bundle.unmapped_entities[0]["canonical_name"] == "벌금"
    assert bundle.relations[0]["relation_type_id"] == stable_id(
        "relation_type", "부과하다"
    )


def test_graph_mapping_requires_every_relation_to_map() -> None:
    raw = _raw_record(
        {
            "relation_mention_id": "mention-1",
            "subject": "금감원",
            "predicate": "새로운행위를한다",
            "object": "벌금",
        }
    )

    with pytest.raises(UnmappedRelationError) as exc_info:
        map_raw_svo_to_graph(
            [raw], _entity_dictionary(), _relation_dictionary(), dictionary_version="v1"
        )

    assert exc_info.value.predicates == ("새로운행위를한다",)


def test_graph_mapping_accepts_only_override_to_existing_relation_type() -> None:
    raw = _raw_record(
        {
            "relation_mention_id": "mention-1",
            "subject": "금감원",
            "predicate": "벌과금을 물린다",
            "object": "벌금",
        }
    )
    relation_type_id = stable_id("relation_type", "부과하다")

    bundle = map_raw_svo_to_graph(
        [raw],
        _entity_dictionary(),
        _relation_dictionary(),
        dictionary_version="v1",
        relation_overrides={"벌과금을 물린다": relation_type_id},
    )

    assert bundle.relation_mapping_results[0]["mapping_status"] == (
        "forced_dictionary_mapping"
    )
    assert bundle.relations[0]["relation_type_id"] == relation_type_id


def test_entity_dictionary_rejects_alias_collision() -> None:
    dictionary = _entity_dictionary() + [
        {
            "canonical_name": "다른기관",
            "aliases": [{"name": "금감원"}],
            "entity_id": stable_id("entity", "다른기관"),
        }
    ]

    with pytest.raises(DictionaryConflictError):
        build_entity_alias_index(dictionary)


def test_same_raw_entity_name_has_one_id_across_documents() -> None:
    first = _raw_record(
        {
            "relation_mention_id": "mention-1",
            "subject": "위원회",
            "predicate": "부과한다",
            "object": "벌금",
        }
    )
    second = dict(first)
    second.update(
        {
            "semantic_unit_id": "unit-2",
            "document_id": "doc-2",
            "document_title": "다른법",
        }
    )
    second["relations"] = [dict(first["relations"][0], relation_mention_id="mention-2")]

    bundle = map_raw_svo_to_graph(
        [first, second], [], _relation_dictionary(), dictionary_version="v1"
    )

    committee = next(entity for entity in bundle.entities if entity["canonical_name"] == "위원회")
    assert committee["entity_id"] == stable_id("entity", "위원회")
    assert committee["mention_count"] == 2


def test_self_reference_uses_document_title_without_becoming_global_alias() -> None:
    raw = _raw_record(
        {
            "relation_mention_id": "mention-1",
            "subject": "이 법",
            "predicate": "부과한다",
            "object": "벌금",
        }
    )

    bundle = map_raw_svo_to_graph(
        [raw], [], _relation_dictionary(), dictionary_version="v1"
    )

    law = next(entity for entity in bundle.entities if entity["canonical_name"] == "테스트법")
    assert law["entity_id"] == stable_id("entity", "테스트법")
    assert [alias["name"] for alias in law["aliases"]] == ["테스트법"]
    mapping = next(
        item for item in bundle.entity_mapping_results if item["raw_name"] == "이 법"
    )
    assert mapping["resolved_name"] == "테스트법"
    assert mapping["mapping_status"] == "current_document_self_reference"
