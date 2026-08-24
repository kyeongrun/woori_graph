import json

from woori_graph.entity_clustering import (
    build_direct_entity_alias_map,
    build_clustered_entity_dictionary,
    propose_entity_mapping,
)


def _entity(name: str, count: int = 1, *, scope: dict | None = None) -> dict:
    return {
        "entity_id": f"source-{name}",
        "canonical_name": name,
        "scope": scope or {"type": "global"},
        "aliases": [],
        "sample_source_refs": [],
        "mention_count": count,
    }


def test_entity_mapping_and_dictionary_merge_aliases() -> None:
    class FakeClient:
        def complete(self, prompt: str) -> str:
            assert "금감원" in prompt
            return json.dumps(
                {
                    "items": [
                        {"alias": "금감원", "canonical_name": "금융감독원"},
                        {"alias": "금융감독원", "canonical_name": "금융감독원"},
                    ]
                },
                ensure_ascii=False,
            )

    records = [_entity("금감원", 2), _entity("금융감독원", 3)]
    mapping, errors = propose_entity_mapping(records, FakeClient(), batch_size=10)
    dictionary = build_clustered_entity_dictionary(records, mapping)

    assert errors == []
    assert len(dictionary) == 1
    assert dictionary[0]["canonical_name"] == "금융감독원"
    assert dictionary[0]["mention_count"] == 5
    assert dictionary[0]["aliases"][0]["name"] == "금융감독원"
    assert dictionary[0]["aliases"][0]["is_canonical"] is True
    assert any(alias["name"] == "금감원" for alias in dictionary[0]["aliases"])


def test_legacy_document_scoped_generic_entities_merge_by_name() -> None:
    records = [
        _entity("위원회", scope={"type": "document", "document_id": "doc-a"}),
        _entity("위원회", scope={"type": "document", "document_id": "doc-b"}),
    ]

    dictionary = build_clustered_entity_dictionary(records, {})

    assert len(dictionary) == 1
    assert dictionary[0]["canonical_name"] == "위원회"
    assert dictionary[0]["mention_count"] == 2
    assert "scope" not in dictionary[0]


def test_explicit_entity_overrides_win_and_put_canonical_alias_first() -> None:
    records = [
        _entity("감사위원회 위원"),
        _entity("감사위원"),
        _entity("경고 등 적절한 조치"),
        _entity("경고조치"),
    ]

    dictionary = build_clustered_entity_dictionary(records, {})

    audit_member = next(item for item in dictionary if item["canonical_name"] == "감사위원")
    warning = next(item for item in dictionary if item["canonical_name"] == "경고")
    assert audit_member["aliases"][0]["name"] == "감사위원"
    assert {item["name"] for item in audit_member["aliases"]} == {
        "감사위원",
        "감사위원회 위원",
    }
    assert warning["aliases"][0]["name"] == "경고"
    assert {item["name"] for item in warning["aliases"]} == {
        "경고",
        "경고조치",
        "경고 등 적절한 조치",
    }


def test_cross_entity_alias_conflict_has_one_deterministic_owner() -> None:
    first = _entity("보수", 2)
    first["aliases"] = [{"name": "보수 지급", "mention_count": 1}]
    second = _entity("보수 지급", 3)

    dictionary = build_clustered_entity_dictionary(
        [first, second],
        {},
        canonical_overrides={},
    )

    owners = [
        record["canonical_name"]
        for record in dictionary
        if any(alias["name"] == "보수 지급" for alias in record["aliases"])
    ]
    assert owners == ["보수 지급"]


def test_explicit_override_injects_curated_alias_not_observed_in_corpus() -> None:
    dictionary = build_clustered_entity_dictionary([_entity("감사의 목적")], {})

    assert [alias["name"] for alias in dictionary[0]["aliases"]] == [
        "감사의 목적",
        "감사의 목적, 필요성",
    ]


def test_final_dictionary_flattens_to_direct_alias_map() -> None:
    dictionary = [
        {
            "entity_id": "entity-1",
            "canonical_name": "금융감독원",
            "entity_type": "ORGANIZATION",
            "aliases": [
                {"name": "금융감독원", "mention_count": 2, "sample_source_refs": []},
                {"name": "금감원", "mention_count": 1, "sample_source_refs": []},
            ],
        }
    ]

    rows = build_direct_entity_alias_map(dictionary)

    assert [row["alias"] for row in rows] == ["금감원", "금융감독원"]
    assert {row["entity_id"] for row in rows} == {"entity-1"}
    assert all(row["entity_type"] == "ORGANIZATION" for row in rows)
