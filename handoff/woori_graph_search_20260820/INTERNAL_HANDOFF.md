# 내부망 이관 묶음: graph search

이 디렉터리는 내부 다중 API 서버에 통합할 최신 소스와 동일 60개 법령 재현 자료를 담는다. API 라우터는 포함하지 않으며, 내부 코드 어시스턴트는 아래 application service와 repository 포트를 내부 프로젝트 구조에 연결한다.

## 업무별 진입점

- `src/woori_graph/dictionary_build/`: 분할 → raw SVO → 엔티티·관계 사전 구축
- `src/woori_graph/graph_build/`: 신규 문서 분할 → raw SVO → 확정 사전 매핑 → 저장소 중립 적재 레코드
- `src/woori_graph/search/`: 질문 임베딩 → OpenSearch 후보 검색 → PostgreSQL 최대 3-hop 탐색 → 근거 답변·시각화
- `src/woori_graph/storage/`: 동일 UUID 기반 RDB·AGE·OpenSearch 적재 파일 생성

검색 API에서는 `SearchApplication.search()`를 호출할 수 있다. 내부 DB 접근 방식이 다르면 `CandidateRepository`와 `GraphRepository` 구현만 교체하고 `GraphSearchPipeline`은 유지한다. 설정의 상대경로는 설정 파일 디렉터리를 기준으로 해석된다.

## 포함 자료

- `src/`, `tests/`, `prompts/`, `config/`, `docs/`, `deploy/`
- `data/raw/`: 외부망과 동일한 공개 법령 60개
- `data/taxonomy/`: 관계 taxonomy 보조 자료
- `runs/v3_scopefree_20260820/final/`: 감사 통과 사전·SVO 기준 자료
- `examples/cross-document-qa-v1/final/`: 서로 다른 법령을 연결하는 질문·답변 10개와 검증 manifest
- `requirements-lock.txt`: Python 3.12/Windows x86-64 고정 의존성
- `wheelhouse/`: 위 lock에 대응하는 폐쇄망 설치 wheel

실제 `.env`, `.git`, 런타임 DB, 로그, cache, 임시·retry·pilot 산출물, 회사 내부규정, 외부망 전용 일회성 비교 코드는 포함하지 않는다.

## 폐쇄망 설치

Python 3.12 x86-64 환경에서 다음 순서로 설치한다.

```powershell
py -3.12 -m pip install --no-index --find-links wheelhouse -r requirements-lock.txt
py -3.12 -m pip install --no-index --no-build-isolation --no-deps .
py -3.12 -m pytest -q -p no:cacheprovider
```

`.env.example`을 복사해 내부 secret 값과 endpoint를 설정한다. 검색 질문 임베딩 모델·차원은 OpenSearch에 적재한 entity/relation 벡터와 같아야 한다. 현재 검증 기준은 `text-embedding-3-small`, 1536차원이지만 완전 폐쇄망에서는 전체 벡터를 동일한 로컬 모델로 재생성해야 한다.

로컬 compose는 개발 검증용이다. 내부 환경에서는 `GRAPH_POSTGRES_DSN`, OpenSearch URL·인증과 컨테이너 보안 설정을 내부 표준으로 교체한다. RDB UUID, AGE의 `id` property, OpenSearch `_id`에는 동일한 UUID 문자열을 사용한다.

## 검증 기준

- 전체 프로젝트 테스트 75개 통과
- 실데이터 단일 질문: 3-hop, 50개 경로, timeout 없음
- 교차 법령 질문: 10개 중 10개에서 2개 이상 문서 근거 경로 확인
- RDB/AGE/OpenSearch 엔티티·관계 UUID 누락 0건

세부 구조와 실행법은 `docs/closed-network-source-structure.ko.md`, `docs/search-pipeline.ko.md`, `deploy/local-storage/README.ko.md`를 따른다.
