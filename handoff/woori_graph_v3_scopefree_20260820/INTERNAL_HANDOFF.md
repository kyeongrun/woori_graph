# 내부망 이관 묶음

이 디렉터리는 내부 다중 API 서버에 통합할 소스와 동일 60개 문서 재실행 자료를 담는다.

- `src/woori_graph/dictionary_build/`: 사전 구축 업무 진입점
- `src/woori_graph/graph_build/`: 신규 문서 그래프 구축 업무 진입점
- `prompts/`: 실행 역할별 프롬프트
- `config/entity_normalization_overrides.jsonl`: 사람이 확정한 엔티티 대표명 기준점
- `data/raw/`: 외부망과 동일한 60개 원문 재실행 자료
- `data/taxonomy/`: 관계 taxonomy 보조 자료
- `runs/v3_scopefree_20260820/final/`: 감사 통과 최종 결과
- `docs/closed-network-source-structure.ko.md`: 내부 서버 연결 지점

실제 `.env`, 이전 run의 `work/`, 캐시, 로그, `.git`, 외부망 전용 DB/API 연결은 포함하지 않았다. `.env.example`을 내부 환경에 맞춰 별도로 구성한다. 실제 RDB·AGE·OpenSearch 적재기는 내부 서버 구조에 맞춰 연결하되, `GraphLoadBundle`의 동일 UUID 문자열을 세 저장소에서 그대로 사용한다.

프로젝트 그래프 테스트는 59개가 통과했다. 작업 중 별도로 추가된 `embedding_compare.py`와 `tests/test_embedding_compare.py`는 이 그래프 파이프라인과 무관하고 현재 프로젝트 Python 경로에서 독립 테스트가 수집되지 않아 이관 묶음에서 제외했다.
