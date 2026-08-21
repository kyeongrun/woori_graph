import json

from woori_graph.normalization import build_first_pass_normalization, propose_relation_mapping


def _record(unit_id: str, title: str, subject: str, predicate: str, object_: str) -> dict:
    return {
        "semantic_unit_id": unit_id,
        "document_id": f"doc-{title}",
        "document_title": title,
        "context_text": "",
        "unit_text": f'{title}의 장(이하 "원장"이라 한다)은 업무를 처리한다.',
        "source_ref": {"article": "제1조", "paragraph": 1, "item_path": []},
        "relations": [
            {
                "relation_mention_id": f"mention-{unit_id}",
                "subject": subject,
                "predicate": predicate,
                "object": object_,
            }
        ],
    }


def test_first_pass_resolves_self_reference_without_global_alias() -> None:
    records = [
        _record("unit-1", "테스트법", "이 법", "정하여야 한다", "기준"),
        _record("unit-2", "테스트법", "이 법", "정한다", "기준"),
    ]
    relation_mapping = {
        "정하여야 한다": {"canonical_name": "정하다", "polarity": "POSITIVE"},
        "정한다": {"canonical_name": "정하다", "polarity": "POSITIVE"},
    }

    entities, relations, edges = build_first_pass_normalization(records, relation_mapping)

    law = next(entity for entity in entities if entity["canonical_name"] == "테스트법")
    assert law["aliases"][0]["name"] == "테스트법"
    assert all(alias["name"] != "이 법" for alias in law["aliases"])
    assert len(relations) == 1
    assert len(edges) == 1
    assert edges[0]["evidence_count"] == 2


def test_self_reference_resolves_to_each_current_document() -> None:
    records = [
        _record("unit-1", "가법", "이 법", "정한다", "기준"),
        _record("unit-2", "나법", "이 법", "정한다", "기준"),
    ]
    relation_mapping = {"정한다": {"canonical_name": "정하다", "polarity": "POSITIVE"}}

    entities, _, _ = build_first_pass_normalization(records, relation_mapping)

    laws = [entity for entity in entities if entity["canonical_name"] in {"가법", "나법"}]
    assert {entity["canonical_name"] for entity in laws} == {"가법", "나법"}
    assert all(entity["mention_count"] == 1 for entity in laws)
    assert all(
        all(alias["name"] != "이 법" for alias in entity["aliases"])
        for entity in laws
    )


def test_same_generic_role_is_globally_merged_without_document_scope() -> None:
    records = [
        _record("unit-1", "가법", "위원회", "처리한다", "신고"),
        _record("unit-2", "나법", "위원회", "처리한다", "신고"),
    ]
    relation_mapping = {"처리한다": {"canonical_name": "처리하다", "polarity": "POSITIVE"}}

    entities, _, _ = build_first_pass_normalization(records, relation_mapping)

    committees = [entity for entity in entities if entity["canonical_name"] == "위원회"]
    assert len(committees) == 1
    assert committees[0]["mention_count"] == 2
    assert "scope" not in committees[0]


def test_invalid_negative_proposal_falls_back_without_id_collision() -> None:
    class InvalidPolarityClient:
        def complete(self, prompt: str) -> str:
            del prompt
            return json.dumps(
                {
                    "items": [
                        {
                            "alias": "주어서는 아니 된다",
                            "canonical_name": "주다",
                            "polarity": "NEGATIVE",
                        }
                    ]
                },
                ensure_ascii=False,
            )

    records = [_record("unit-1", "테스트법", "위원회", "주어서는 아니 된다", "자료")]
    mapping, errors = propose_relation_mapping(records, InvalidPolarityClient())

    assert errors == []
    assert mapping["주어서는 아니 된다"] == {
        "canonical_name": "주어서는 아니 된다",
        "polarity": "NEGATIVE",
        "normalization_status": "fallback_raw_invalid_proposal",
    }


def test_prohibition_action_is_positive_not_grammatical_negation() -> None:
    class ProhibitionClient:
        def complete(self, prompt: str) -> str:
            del prompt
            return json.dumps(
                {
                    "items": [
                        {
                            "alias": "금지할 수 있다",
                            "canonical_name": "금지하다",
                            "polarity": "POSITIVE",
                        }
                    ]
                },
                ensure_ascii=False,
            )

    records = [_record("unit-1", "테스트법", "위원회", "금지할 수 있다", "거래")]
    mapping, errors = propose_relation_mapping(records, ProhibitionClient())

    assert errors == []
    assert mapping["금지할 수 있다"]["canonical_name"] == "금지하다"
    assert mapping["금지할 수 있다"]["polarity"] == "POSITIVE"
    assert mapping["금지할 수 있다"]["normalization_status"] == "llm_proposed"


def test_generic_noun_before_local_definition_does_not_replace_defined_name() -> None:
    record = _record("unit-1", "신탁법", "수탁자", "관리한다", "재산")
    record["unit_text"] = '신탁을 인수하는 자(이하 "수탁자"라 한다)는 재산을 관리한다.'

    entities, _, _ = build_first_pass_normalization(
        [record],
        {"관리한다": {"canonical_name": "관리하다", "polarity": "POSITIVE"}},
    )

    assert any(entity["canonical_name"] == "수탁자" for entity in entities)
    assert not any(
        entity["canonical_name"] == "자"
        and any(alias["name"] == "수탁자" for alias in entity["aliases"])
        for entity in entities
    )


def test_specific_institution_name_does_not_resolve_generic_abbreviation() -> None:
    record = _record("unit-1", "권익법", "위원회", "처리한다", "신고")
    record["unit_text"] = '국민권익위원회(이하 "위원회"라 한다)는 신고를 처리한다.'

    entities, _, _ = build_first_pass_normalization(
        [record],
        {"처리한다": {"canonical_name": "처리하다", "polarity": "POSITIVE"}},
    )

    committee = next(entity for entity in entities if entity["canonical_name"] == "위원회")
    assert committee["aliases"][0]["name"] == "위원회"
    assert not any(entity["canonical_name"] == "국민권익위원회" for entity in entities)
