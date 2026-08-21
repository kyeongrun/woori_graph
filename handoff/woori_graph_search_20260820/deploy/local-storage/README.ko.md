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
  --raw-svo runs/v3_scopefree_20260820/final/raw_svo.jsonl `
  --entities runs/v3_scopefree_20260820/final/entity_dictionary.jsonl `
  --relations runs/v3_scopefree_20260820/final/relation_dictionary.jsonl `
  --dictionary-version v3-scopefree-20260820 `
  --age-graph-name svo `
  --output runtime/load/v3_scopefree_20260820
```

Docker Desktop을 시작한 다음 적재한다.

```powershell
.\deploy\local-storage\load.ps1 -ReleaseName v3_scopefree_20260820
```

OpenSearch bulk 전송처럼 후반 단계만 실패했고 RDB·AGE가 이미 정상 적재된 경우에는 삭제 없이 다음 명령으로 재개한다.

```powershell
.\deploy\local-storage\load.ps1 -ReleaseName v3_scopefree_20260820 -Resume
```

임베딩 모델이나 벡터 차원이 바뀌어 OpenSearch 인덱스만 다시 만들 때는 RDB·AGE를 유지하고 다음 명령을 사용한다.

```powershell
.\deploy\local-storage\load.ps1 -ReleaseName v3_scopefree_20260820 -ReplaceOpenSearch
```

동일한 버전을 의도적으로 다시 적재할 때만 `-Replace`를 사용한다. 이 옵션은 해당 버전의 AGE graph와 OpenSearch version index를 교체한다. RDB는 동일 UUID 기준 upsert한다.

정상 완료 시 적재 폴더에 `load_reconcile.json`이 생성된다. RDB, AGE, OpenSearch의 레코드 수와 RDB/AGE UUID 전수 대조가 모두 일치해야 `passed: true`가 된다.

## 데이터 모델

- PostgreSQL DB: `graphdb`; RDB schema: `graph`
- RDB: `graph.document`, `entity`, `relation_type`, `relation`, `relation_evidence`
- AGE: `Entity` vertex와 canonical relation name별 edge label. vertex property는 `id`, `name`; edge property는 `id`, `source_name`, `target_name`만 둔다.
- AGE graph: `svo`
- OpenSearch: 버전별 `entities-<버전>`, `relations-<버전>` index와 고정 `entities`, `relations` alias

AGE의 내부 graph ID는 AGE가 요구하는 숫자다. 애플리케이션 식별자는 vertex/edge의 `id` property이며 RDB UUID와 OpenSearch `_id`에 사용한 UUID 문자열과 정확히 같다.
