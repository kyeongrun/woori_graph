import json

from woori_graph.extraction import (
    align_raw_svo_records,
    OpenAICompatConfig,
    extract_raw_svo,
    extract_units,
    sanitize_raw_svo_records,
)
from woori_graph.models import SemanticUnit, SourceRef


class FakeClient:
    def __init__(self, response: str):
        self.response = response

    def complete(self, prompt: str) -> str:
        assert "unit_text" in prompt
        return self.response


def _unit() -> SemanticUnit:
    return SemanticUnit(
        semantic_unit_id="f4dd686a-41c3-5aa5-b8f2-548effb165ac",
        document_id="b0622b0e-92d8-5c3b-919d-e9801104404a",
        document_title="테스트법",
        source_path="테스트법/법률.md",
        source_ref=SourceRef(article="제1조", paragraph=1, item_path=()),
        context_text="##### 제1조 (목적)",
        unit_text="위원회는 보고서를 제출한다.",
        unit_kind="paragraph",
    )


def test_raw_extraction_validates_and_deduplicates_relations() -> None:
    response = json.dumps(
        {
            "relations": [
                {"subject": "위원회", "predicate": "제출한다", "object": "보고서"},
                {"subject": "위원회", "predicate": "제출한다", "object": "보고서"},
            ]
        }
    )

    relations = extract_raw_svo(_unit(), FakeClient(response))

    assert len(relations) == 1
    assert relations[0].relation_mention_id == "6dad5939-992c-5d8b-9a81-77827a360309"
    assert relations[0].subject == "위원회"


def test_batch_extraction_discards_incomplete_relation_objects() -> None:
    records, errors = extract_units(
        [_unit()],
        FakeClient('{"relations": [{"subject": "위원회"}]}'),
    )

    assert records[0]["semantic_unit_id"] == _unit().semantic_unit_id
    assert records[0]["relations"] == []
    assert errors == []


def test_openai_compat_config_reads_thinking_mode(monkeypatch) -> None:
    monkeypatch.setenv("VLLM_ENABLE_THINKING", "true")

    config = OpenAICompatConfig.from_env()

    assert config.enable_thinking is True


def test_raw_extraction_removes_quantity_qualifiers_from_endpoints() -> None:
    response = json.dumps(
        {
            "relations": [
                {
                    "subject": "금융감독원",
                    "predicate": "부과한다",
                    "object": "500만원 이하의 과태료",
                },
                {
                    "subject": "회사",
                    "predicate": "제출한다",
                    "object": "2년간의 사업계획서",
                },
            ]
        },
        ensure_ascii=False,
    )

    relations = extract_raw_svo(_unit(), FakeClient(response))

    assert [(item.subject, item.predicate, item.object) for item in relations] == [
        ("금융감독원", "부과한다", "과태료"),
        ("회사", "제출한다", "사업계획서"),
    ]


def test_raw_extraction_can_preserve_llm_output_without_endpoint_rules() -> None:
    response = json.dumps(
        {
            "relations": [
                {
                    "subject": "제1항제1호의 경우 금융지주회사와 그 자회사등",
                    "predicate": "보지 아니한다",
                    "object": "500만원 이하의 금액 및 그 밖의 사항",
                }
            ]
        },
        ensure_ascii=False,
    )

    relations = extract_raw_svo(
        _unit(),
        FakeClient(response),
        preserve_llm_output=True,
    )

    assert [(item.subject, item.predicate, item.object) for item in relations] == [
        (
            "제1항제1호의 경우 금융지주회사와 그 자회사등",
            "보지 아니한다",
            "500만원 이하의 금액 및 그 밖의 사항",
        )
    ]


def test_raw_extraction_drops_pure_condition_and_generic_endpoints() -> None:
    response = json.dumps(
        {
            "relations": [
                {"subject": "회사", "predicate": "유지한다", "object": "1년"},
                {"subject": "위원회", "predicate": "처리한다", "object": "사항"},
            ]
        },
        ensure_ascii=False,
    )

    assert extract_raw_svo(_unit(), FakeClient(response)) == []


def test_raw_extraction_splits_clear_endpoint_enumerations() -> None:
    response = json.dumps(
        {
            "relations": [
                {
                    "subject": "위원회 또는 회사",
                    "predicate": "관리한다",
                    "object": "인력 및 물적 설비",
                }
            ]
        },
        ensure_ascii=False,
    )

    relations = extract_raw_svo(_unit(), FakeClient(response))

    assert {(item.subject, item.predicate, item.object) for item in relations} == {
        ("위원회", "관리한다", "인력"),
        ("위원회", "관리한다", "물적 설비"),
        ("회사", "관리한다", "인력"),
        ("회사", "관리한다", "물적 설비"),
    }


def test_raw_extraction_splits_comma_and_middle_dot_enumerations() -> None:
    response = json.dumps(
        {
            "relations": [
                {
                    "subject": "임원, 직원ㆍ담당자",
                    "predicate": "열람한다",
                    "object": "보고서·장부",
                }
            ]
        },
        ensure_ascii=False,
    )

    relations = extract_raw_svo(_unit(), FakeClient(response))

    assert {(item.subject, item.object) for item in relations} == {
        (subject, object_)
        for subject in ("임원", "직원", "담당자")
        for object_ in ("보고서", "장부")
    }


def test_raw_extraction_drops_condition_only_members_from_enumeration() -> None:
    response = json.dumps(
        {
            "relations": [
                {
                    "subject": "회사",
                    "predicate": "부과한다",
                    "object": "500만원 이하의 과태료 및 1년",
                }
            ]
        },
        ensure_ascii=False,
    )

    relations = extract_raw_svo(_unit(), FakeClient(response))

    assert [(item.subject, item.predicate, item.object) for item in relations] == [
        ("회사", "부과한다", "과태료")
    ]


def test_raw_extraction_preserves_single_legal_name_with_internal_conjunction() -> None:
    response = json.dumps(
        {
            "relations": [
                {
                    "subject": "회사",
                    "predicate": "준수한다",
                    "object": "자본시장과 금융투자업에 관한 법률",
                }
            ]
        },
        ensure_ascii=False,
    )

    relations = extract_raw_svo(_unit(), FakeClient(response))

    assert relations[0].object == "자본시장과 금융투자업에 관한 법률"


def test_raw_extraction_does_not_split_a_noun_ending_in_gwa() -> None:
    response = json.dumps(
        {
            "relations": [
                {
                    "subject": "감사위원회",
                    "predicate": "결정한다",
                    "object": "중요 감사 결과 등 보고에 관한 사항",
                }
            ]
        },
        ensure_ascii=False,
    )

    relations = extract_raw_svo(_unit(), FakeClient(response))

    assert relations[0].object == "중요 감사 결과 등 보고에 관한 사항"


def test_raw_extraction_collapses_detailed_qualification_to_person() -> None:
    response = json.dumps(
        {
            "relations": [
                {
                    "subject": "위원회",
                    "predicate": "임용한다",
                    "object": "감사ㆍ수사 업무를 3년 이상 담당한 사람으로서 5급 이상 공무원으로 근무한 경력이 있는 사람",
                }
            ]
        },
        ensure_ascii=False,
    )

    relations = extract_raw_svo(_unit(), FakeClient(response))

    assert relations[0].object == "사람"


def test_raw_extraction_preserves_retirement_period_person_description() -> None:
    response = json.dumps(
        {
            "relations": [
                {
                    "subject": "위원회",
                    "predicate": "임용한다",
                    "object": "단체의 임직원으로 근무하다가 퇴직한 후 2년이 지나지 아니한 사람",
                }
            ]
        },
        ensure_ascii=False,
    )

    relations = extract_raw_svo(_unit(), FakeClient(response))

    assert relations[0].object == "단체의 임직원으로 근무하다가 퇴직한 후 2년이 지나지 아니한 사람"


def test_raw_extraction_preserves_retirement_state_even_with_work_history() -> None:
    response = json.dumps(
        {
            "relations": [
                {
                    "subject": "위원회",
                    "predicate": "제한한다",
                    "object": "금융회사에서 근무한 후 퇴직일부터 2년이 지나지 않은 사람",
                }
            ]
        },
        ensure_ascii=False,
    )

    relations = extract_raw_svo(_unit(), FakeClient(response))

    assert relations[0].object == "금융회사에서 근무한 후 퇴직일부터 2년이 지나지 않은 사람"


def test_raw_extraction_repeats_shared_genitive_for_enumerated_entities() -> None:
    response = json.dumps(
        {
            "relations": [
                {
                    "subject": "감사인",
                    "predicate": "설명한다",
                    "object": "감사의 목적, 필요성",
                }
            ]
        },
        ensure_ascii=False,
    )

    relations = extract_raw_svo(_unit(), FakeClient(response))

    assert {(item.subject, item.object) for item in relations} == {
        ("감사인", "감사의 목적"),
        ("감사인", "감사의 필요성"),
    }


def test_stored_raw_svo_can_be_resanitized_with_stable_mention_id() -> None:
    record = _unit().to_dict()
    record["relations"] = [
        {
            "relation_mention_id": "old-id",
            "subject": "위원회",
            "predicate": "부과한다",
            "object": "500만원 이하의 과태료",
        }
    ]

    sanitized = sanitize_raw_svo_records([record])

    assert sanitized[0]["relations"][0]["object"] == "과태료"
    assert sanitized[0]["relations"][0]["relation_mention_id"] != "old-id"


def test_raw_svo_records_can_be_restored_to_semantic_unit_order() -> None:
    records = [
        {"semantic_unit_id": "unit-b", "relations": []},
        {"semantic_unit_id": "unit-a", "relations": []},
    ]

    aligned = align_raw_svo_records(["unit-a", "unit-b"], records)

    assert [record["semantic_unit_id"] for record in aligned] == ["unit-a", "unit-b"]


def test_raw_extraction_removes_inline_grade_and_drops_pure_score() -> None:
    response = json.dumps(
        {
            "relations": [
                {
                    "subject": "보건복지부의 3급 공무원",
                    "predicate": "평가한다",
                    "object": "보고서",
                },
                {
                    "subject": "위원회",
                    "predicate": "부여한다",
                    "object": "40점",
                },
            ]
        },
        ensure_ascii=False,
    )

    relations = extract_raw_svo(_unit(), FakeClient(response))

    assert [(item.subject, item.object) for item in relations] == [
        ("보건복지부의 공무원", "보고서")
    ]


def test_raw_extraction_preserves_non_retirement_period_person_state() -> None:
    response = json.dumps(
        {
            "relations": [
                {
                    "subject": "위원회",
                    "predicate": "제한한다",
                    "object": "문책을 받은 날부터 2년이 지나지 아니한 사람",
                }
            ]
        },
        ensure_ascii=False,
    )

    relations = extract_raw_svo(_unit(), FakeClient(response))

    assert relations[0].object == "문책을 받은 날부터 2년이 지나지 아니한 사람"


def test_inline_quantity_cleanup_does_not_damage_law_article_ranges() -> None:
    response = json.dumps(
        {
            "relations": [
                {
                    "subject": "회사",
                    "predicate": "준수한다",
                    "object": "「상법」 제335조의2부터 제335조의7까지",
                }
            ]
        },
        ensure_ascii=False,
    )

    relations = extract_raw_svo(_unit(), FakeClient(response))

    assert relations[0].object == "「상법」 제335조의2부터 제335조의7까지"
