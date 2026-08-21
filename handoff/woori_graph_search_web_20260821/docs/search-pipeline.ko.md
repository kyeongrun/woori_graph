# 그래프 탐색 파이프라인

탐색은 사전 구축 및 그래프 구축과 분리된 독립 애플리케이션 파이프라인이다.

```text
질문
  → 질문 임베딩
  → OpenSearch entity/relation 키워드·벡터 후보 검색
  → RRF 후보 결합
  → PostgreSQL relation 인접 탐색(최대 3-hop)
  → 질문 방향·관계 유사도 기반 beam ranking
  → RDB 문서·조문 근거 결합
  → 근거 기반 답변 + graph.json + 독립 graph.html
```

`src/woori_graph/search/`는 다음 경계로 분리한다.

- `pipeline.py`: 저장소 중립 탐색 규칙과 3-hop beam search
- `repositories.py`: `CandidateRepository`, `GraphRepository` 포트와 현재 OpenSearch/PostgreSQL 어댑터
- `application.py`: CLI 또는 내부 API가 재사용하는 composition root
- `artifacts.py`: 표준 `work/`/`final/` 탐색 산출물과 무의존성 HTML 그래프
- `cross_document.py`: 서로 다른 문서 근거를 갖는 질문 발견 및 검증
- `config.py`: 설정 파일 기준 상대경로 해석과 180초 상한 검증

내부 API 서버로 이식할 때 `GraphSearchPipeline`은 변경하지 않고, 내부 연결 방식에 맞는 두 repository 포트 구현만 교체할 수 있다. 현재 PostgreSQL 어댑터는 RDB relation index로 인접 관계를 찾는다. 필요하면 같은 포트 아래 AGE 기반 어댑터로 교체할 수 있으며 공개 UUID 계약은 바뀌지 않는다.

## 설정

예제는 `config/search.example.toml`이다. 비밀값은 TOML에 쓰지 않는다.

```env
GRAPH_POSTGRES_DSN=postgresql://woori@127.0.0.1:55432/graphdb
EMBEDDING_BASE_URL=https://api.openai.com/v1
EMBEDDING_API_KEY=...
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIMENSION=1536
```

질문 임베딩 모델은 적재 벡터를 만든 모델과 같아야 한다. 완전한 폐쇄망으로 이전할 때 모델을 변경하면 전체 entity/relation 벡터를 같은 로컬 모델로 다시 생성해야 한다.

## 단일 질문 실행

```powershell
woori-graph search-query `
  --config config/search.example.toml `
  --query "금융위원회와 금융기관은 어떻게 연결되는가?" `
  --run-id query-example `
  --allow-remote-embedding
```

표준 출력은 `artifacts/search/<run-id>/` 아래에 생성된다.

```text
work/
  01_candidates.json
  02_ranked_paths.json
final/
  query_result.json
  answer.md
  graph.json
  graph.html
  search_manifest.json
```

`graph.html`은 CDN이나 서버 API 호출이 없는 단일 파일이다. 노드 또는 관계를 선택하면 연결 관계와 근거 문서를 확인할 수 있다.

## 질문형 웹 화면

로컬에서 질문을 직접 입력하고 답변·그래프·근거·탐색 진단을 한 화면에서 확인하려면 다음 명령을 실행한다.

```powershell
woori-graph search-web `
  --config config/search.example.toml `
  --port 8765 `
  --allow-remote-embedding
```

브라우저에서 `http://127.0.0.1:8765/`을 연다. `--open-browser`를 추가하면 기본 브라우저를 자동으로 연다. 보안을 위해 개발용 서버는 loopback 주소만 허용하며, 내부 API 서버에서는 `SearchApplication`과 repository 포트를 기존 서버 프레임워크에 연결한다.

화면에는 다음 정보가 표시된다.

- 질문에 대한 근거 기반 답변과 최대 3-hop 관계 그래프
- 노드·관계 선택에 따라 필터링되는 법령·조문·원표현 근거
- 엔티티 Top-K, 릴레이션 Top-K, 엔티티별 인접 관계 한도, beam width, 최종 경로 수
- hop별 frontier 엔티티 수, RDB에서 조회한 관계 수와 조회 상한, 생성 경로 수, beam 잔존 수
- OpenSearch에서 선정된 엔티티와 릴레이션 전체 목록, 점수, 키워드/벡터 매칭 출처
- 3분 시간 예산에 따라 실제 적용된 top-k·neighbor·beam·경로 축소 내역

현재 기본 설정은 엔티티 Top-8, 릴레이션 Top-20, 엔티티별 인접 관계 50, hop별 beam 160, 최종 경로 50이다. OpenSearch 후보 단계는 키워드와 벡터 결과를 RRF로 결합한다. 그래프 확장은 **릴레이션 Top-20에 포함된 관계만 따라가는 방식이 아니다.** 각 hop의 frontier 엔티티에 연결된 관계를 RDB에서 조회하고, 질문과의 이름 일치, 릴레이션 후보 관련도, 근거 수, 질문 방향 단서를 점수에 반영한 뒤 엔티티별 neighbor 한도와 beam width로 줄인다. 따라서 릴레이션 후보에 없던 관계도 그래프 확장과 최종 경로에 포함될 수 있다.

## 문서 간 질문 10개 검증

```powershell
woori-graph search-cross-document-qa `
  --config config/search.example.toml `
  --run-id cross-document-qa-v1 `
  --allow-remote-embedding
```

RDB 전체 snapshot에서 서로 다른 `document_id`의 근거를 갖는 2-hop 연결을 찾은 뒤, 각 질문을 일반 탐색 파이프라인으로 다시 실행한다. 성공 조건은 질문에 명시된 시작·중간·끝 엔티티가 한 경로에 있고 그 경로의 근거 문서가 두 개 이상인 것이다. 최초 발견 relation UUID도 감사용으로 보존한다.

## 3분 시간 제어

- 설정의 `timeout_seconds`는 최대 180초이며 더 큰 값은 거부한다.
- 전체 요청에 하나의 monotonic deadline을 사용한다.
- 경과 시간이 예산의 67%를 넘으면 entity/relation top-k, hop별 neighbor, beam width, evidence 수와 최종 path 수를 절반으로 줄인다.
- 84%를 넘으면 같은 값을 1/4로 줄인다.
- deadline에 도달하면 외부 호출을 추가하지 않고 근거가 확보된 부분 결과와 `timed_out=true`를 반환한다.
- 적용된 축소는 결과와 manifest의 `stats.adaptations`에 기록한다.
