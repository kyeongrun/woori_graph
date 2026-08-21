from woori_graph.dictionary_build.seeding import (
    build_refreshed_relation_dictionary,
    build_seeded_entity_mapping,
)
from woori_graph.ids import stable_id


def test_seeded_entity_mapping_uses_only_unique_aliases_and_ignores_scope() -> None:
    source = [
        {"canonical_name": "금감원"},
        {"canonical_name": "위원회"},
        {"canonical_name": "금융감독원"},
    ]
    seed = [
        {
            "canonical_name": "금융감독원",
            "aliases": [{"name": "금감원", "scope": {"type": "global"}}],
        },
        {
            "canonical_name": "국민권익위원회",
            "aliases": [{"name": "위원회", "scope": {"type": "document"}}],
        },
        {
            "canonical_name": "관리위원회",
            "aliases": [{"name": "위원회", "scope": {"type": "document"}}],
        },
    ]

    mapping = build_seeded_entity_mapping(source, seed)

    assert mapping["금감원"]["canonical_name"] == "금융감독원"
    assert mapping["위원회"]["canonical_name"] == "위원회"
    assert mapping["금융감독원"]["canonical_name"] == "금융감독원"


def test_refreshed_relation_dictionary_keeps_taxonomy_and_adds_forced_alias() -> None:
    relation_type_id = stable_id("relation_type", "부과하다")
    seed = [
        {
            "canonical_name": "부과하다",
            "relation_type_id": relation_type_id,
            "polarity": "POSITIVE",
            "aliases": [{"name": "부과한다"}],
        }
    ]
    raw = [
        {
            "semantic_unit_id": "unit-1",
            "document_id": "doc-1",
            "source_ref": {"article": "제1조", "paragraph": 1, "item_path": []},
            "relations": [
                {"predicate": "벌과금을 물린다"},
                {"predicate": "부과한다"},
            ],
        }
    ]

    refreshed = build_refreshed_relation_dictionary(
        raw,
        seed,
        relation_overrides={"벌과금을 물린다": relation_type_id},
    )

    assert len(refreshed) == 1
    assert refreshed[0]["mention_count"] == 2
    aliases = {item["name"]: item["mention_count"] for item in refreshed[0]["aliases"]}
    assert aliases["벌과금을 물린다"] == 1
    assert aliases["부과한다"] == 1
