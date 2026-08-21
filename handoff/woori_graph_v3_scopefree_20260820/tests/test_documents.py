from woori_graph.documents import segment_text


_SOURCE = """---
제목: 테스트법
법령MST: '999999'
---
# 테스트법
##### 제1조 (처리)
**①** 위원회는 다음 각 호의 업무를 처리한다.
  1\\. 신고를 접수한다.
    가. 접수 서류를 보관한다.
  2\\. 처리 결과를 통지한다.
"""


def test_segmenter_keeps_parent_context_and_item_locations() -> None:
    units = segment_text(_SOURCE, source_path="테스트법/법률.md")

    assert len(units) == 2
    assert units[0].source_ref.item_path == ("1", "가")
    assert units[0].unit_kind == "terminal_item"
    assert "위원회는 다음 각 호의 업무를 처리한다." in units[0].context_text
    assert "신고를 접수한다." in units[0].context_text
    assert units[0].unit_text == "접수 서류를 보관한다."
    assert units[1].source_ref.item_path == ("2",)


def test_nominal_terminal_item_is_one_svo_request_with_parent_predicate() -> None:
    source = """---
제목: 테스트법
법령MST: '999999'
---
# 테스트법
##### 제1조 (업무)
**①** 위원회는 다음 각 호의 업무를 처리한다.
  1\\. 신청서 접수
"""

    units = segment_text(source, source_path="테스트법/법률.md")

    assert len(units) == 1
    assert units[0].unit_kind == "terminal_item"
    assert units[0].unit_text == "신청서 접수"
    assert "위원회는 다음 각 호의 업무를 처리한다." in units[0].context_text
    assert all(unit.unit_text != "위원회는 다음 각 호의 업무를 처리한다." for unit in units)


def test_document_id_uses_official_metadata_not_source_path() -> None:
    from_file_root = segment_text(_SOURCE, source_path="법률.md")
    from_parent_root = segment_text(_SOURCE, source_path="법령/테스트법/법률.md")

    assert from_file_root[0].document_id == from_parent_root[0].document_id


def test_historical_supplements_are_excluded_from_current_body() -> None:
    source = _SOURCE + """
## 부칙 <제123호,2020.1.1>
### 제1조 (시행일)
이 법은 공포한 날부터 시행한다.
"""

    units = segment_text(source, source_path="테스트법/법률.md")

    assert all("공포한 날" not in unit.unit_text for unit in units)


def test_section_heading_does_not_leak_into_preceding_article() -> None:
    source = _SOURCE + """
### 제2절 다른 절
##### 제2조 (다음 조문)
기관은 보고서를 제출한다.
"""

    units = segment_text(source, source_path="테스트법/법률.md")

    assert all("제2절" not in unit.unit_text for unit in units)


def test_source_document_key_keeps_document_id_stable_after_path_move() -> None:
    source = """# 테스트법
##### 제1조 (보고)
위원회는 보고서를 제출한다.
"""

    first = segment_text(
        source,
        source_path="before/test.md",
        source_document_key="law:test:001",
    )
    moved = segment_text(
        source,
        source_path="after/renamed.md",
        source_document_key="law:test:001",
    )

    assert first[0].document_id == moved[0].document_id
    assert first[0].semantic_unit_id == moved[0].semantic_unit_id
