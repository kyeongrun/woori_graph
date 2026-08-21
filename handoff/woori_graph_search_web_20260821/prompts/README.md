# 프롬프트

이 디렉터리의 프롬프트는 v3 설계 지시서를 기준으로 관리한다.

- `raw_svo_extract.ko.md`: 사전 구축과 그래프 구축이 공통으로 사용하는 현재 원시 SVO 추출 프롬프트
- `entity_normalize.ko.md`: 사전 구축의 엔티티 canonical name/alias 정규화 프롬프트
- `entity_contextual_normalize.ko.md`: aliases와 실제 원문을 함께 보는 2차 엔티티 대표명 통합 프롬프트
- `relation_normalize.ko.md`: 원시 술어의 대표 행위·긍정/부정 정규화 프롬프트
- `relation_taxonomy.ko.md`: 100개 이하 폐쇄형 관계 행위군 생성 프롬프트
- `relation_closed_map.ko.md`: 1차 관계 타입을 폐쇄형 행위군에 전수 매핑하는 프롬프트
- `relation_classify.ko.md`: 신규 문서의 미등록 술어를 확정 관계 사전에 강제 매핑하는 프롬프트
- `raw-svo-v3.ko.md`: 2026-08-20 이전 실행과 비교하기 위한 구 이름의 프롬프트
- `raw-svo-v3-repair.ko.md`: 품질 경고 표본의 선택 재추출에 사용한 교정 프롬프트

운영 기본값은 역할 중심 이름인 `raw_svo_extract.ko.md`를 사용한다. 구 이름 파일을 새 실행의 기본 프롬프트로 사용하지 않는다.

이전 v2의 `normative_type`, 도메인 판정, 복합 endpoint 내 법조항의 별도 파생 규칙은 가져오지 않는다.
