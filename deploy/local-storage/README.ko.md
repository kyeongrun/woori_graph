# 로컬 적재

PostgreSQL DB 이름은 `graphdb`, RDB 스키마는 `graph`, AGE graph는 `svo`로 고정한다. 사전 버전은 DB 이름이나 graph 이름에 넣지 않고 적재 manifest에만 기록한다.

RDB와 OpenSearch 적재 필드에는 `dictionary_match`, `dictionary_version`, `mention_count`를 두지 않으며 `load_release` 테이블도 만들지 않는다. `-Replace`는 AGE/OpenSearch뿐 아니라 RDB의 현재 그래프 데이터도 비운 뒤 동일 UUID 기준으로 전체 snapshot을 다시 적재한다.

이 구성은 현재 결과를 탐색하기 위한 로컬 전용 환경이다. PostgreSQL과 Apache AGE는 같은 컨테이너를 사용하고 OpenSearch는 별도 단일 노드로 실행한다.

- PostgreSQL/AGE 영속 데이터: 프로젝트 루트의 `runtime/postgres`
- OpenSearch 영속 데이터: 프로젝트 루트의 `runtime/opensearch`
- 버전별 적재 파일: 프로젝트 루트의 `runtime/load/<release>`
- PostgreSQL: `127.0.0.1:55432`
- OpenSearch: `http://127.0.0.1:19200`

두 포트는 loopback에만 공개한다. PostgreSQL은 로컬 탐색을 위해 trust 인증, OpenSearch는 보안 플러그인 비활성화 상태이므로 외부 서버나 운영 환경에 이 구성을 그대로 사용하면 안 된다.

Compose volume과 `load.ps1`은 스크립트 위치에서 프로젝트 루트를 계산하므로 저장소를 다른 드라이브나 디렉터리로 옮겨도 경로를 수정할 필요가 없다. 내부 API 서버에서는 이 로컬 compose를 그대로 사용하기보다 내부 PostgreSQL/OpenSearch 접속 설정과 보안 정책으로 교체한다.

## 실행

먼저 버전별 적재 파일을 만든다.

```powershell
woori-graph build-load-files `
  --raw-svo runs/v3_scopefree_20260820/work/02_raw_svo.jsonl `
  --entities artifacts/dictionary-build/v3-llm-20260821/final/entity_dictionary.jsonl `
  --relations artifacts/dictionary-build/v3-llm-20260821/final/relation_dictionary.jsonl `
  --dictionary-version v3-llm-20260821 `
  --age-graph-name svo `
  --output runtime/load/v3_llm_20260821 `
  --env-file .env `
  --allow-remote-embedding `
  --embedding-batch-size 128
```

Docker Desktop을 시작한 다음 적재한다.

```powershell
.\deploy\local-storage\load.ps1 -ReleaseName v3_llm_20260821
```

처음 실행할 때는 이 명령이 필요한 Apache AGE와 OpenSearch 이미지를 함께 내려받아 기동한다.

OpenSearch bulk 전송처럼 후반 단계만 실패했고 RDB·AGE가 이미 정상 적재된 경우에는 삭제 없이 다음 명령으로 재개한다.

```powershell
.\deploy\local-storage\load.ps1 -ReleaseName v3_llm_20260821 -Resume
```

임베딩 모델이나 벡터 차원이 바뀌어 OpenSearch 인덱스만 다시 만들 때는 RDB·AGE를 유지하고 다음 명령을 사용한다.

```powershell
.\deploy\local-storage\load.ps1 -ReleaseName v3_llm_20260821 -ReplaceOpenSearch
```

관계와 UUID는 그대로 두고 의미 단위 원문 등 RDB 데이터만 전체 교체할 때는 AGE와 OpenSearch를 유지하고 다음 명령을 사용한다.

```powershell
.\deploy\local-storage\load.ps1 -ReleaseName <새_적재_릴리스> -ReplaceRdbOnly
```

`-ReplaceRdbOnly`도 RDB 내부에서는 `document`, `semantic_unit`, `entity`, `relation_type`, `relation`, `relation_evidence` 전체를 같은 릴리스로 다시 적재한다. 일부 테이블만 교체해서 외래키와 출처가 어긋나는 상태는 허용하지 않는다. 완료 후 AGE·OpenSearch의 기존 UUID와도 다시 대조한다.

동일한 버전을 의도적으로 다시 적재할 때만 `-Replace`를 사용한다. 이 옵션은 해당 버전의 AGE graph와 OpenSearch version index를 교체한다. RDB는 동일 UUID 기준 upsert한다.

정상 완료 시 적재 폴더에 `load_reconcile.json`이 생성된다. RDB, AGE, OpenSearch의 레코드 수, 5개 엔티티 타입별 개수, RDB/AGE UUID 전수 대조가 모두 일치해야 `passed: true`가 된다.

## 데이터 모델

- PostgreSQL DB: `graphdb`; RDB schema: `graph`
- RDB: `graph.document`, `semantic_unit`, `entity`, `relation_type`, `relation`, `relation_evidence`. `semantic_unit`에는 조문 위치(`source_ref`), 직접 원문(`unit_text`), 상위 문맥(`context_text`), 의미 단위 종류를 저장하고 `relation_evidence`가 외래키로 참조한다.
- AGE: `ORGANIZATION`, `PERSON`, `LEGAL_INSTRUMENT`, `CONCEPT`, `OTHER` vertex label과 canonical relation name별 edge label을 사용한다. vertex property는 `id`만, edge property는 `id`, `source_name`, `target_name`만 저장한다. 이름·별칭·타입 메타정보와 전체 원문 근거는 RDB에서, 검색 후보 필드는 OpenSearch에서 같은 UUID로 조회한다.
- AGE graph: `svo`
- OpenSearch: 버전별 `entities-<버전>`, `relations-<버전>` index와 고정 `entities`, `relations` alias

AGE의 내부 graph ID는 AGE가 요구하는 숫자다. 애플리케이션 식별자는 vertex/edge의 `id` property이며 RDB UUID와 OpenSearch `_id`에 사용한 UUID 문자열과 정확히 같다.

## 검색 웹과 함께 실행

로컬 AGE/OpenSearch와 직접 구현한 검색 웹을 한 번에 실행할 수 있다.

```powershell
.\deploy\local-storage\start.ps1
```

`start.ps1`은 로컬 AGE/OpenSearch 컨테이너를 백그라운드로 시작한 뒤, 같은 창에서 검색 웹을 실행한다. 검색 웹은 `Ctrl+C`로 종료하고, 컨테이너까지 종료하려면 `docker compose -f deploy/local-storage/compose.yaml down`을 실행한다.

검색 답변용 LLM은 기본적으로 loopback 주소만 허용한다. `.env`의 `VLLM_BASE_URL`이 사설 원격 주소라면, 그 주소 사용을 명시적으로 승인한 경우에만 다음처럼 실행한다.

```powershell
.\deploy\local-storage\start.ps1 -AllowRemoteLlm
```

`-AllowRemoteLlm`을 주지 않으면 검색 웹은 계속 기동되고, LLM 답변만 결정적 근거 답변으로 fallback한다.

- 검색 웹: `http://127.0.0.1:8765/`
