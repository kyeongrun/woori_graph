# OpenSearch 벡터 자동 적재

## 동작 경계

- 임베딩은 OpenSearch의 `entities.embedding`, `relations.embedding`에만 저장한다.
- RDB와 AGE에는 벡터를 복제하지 않는다.
- entity UUID와 relation UUID는 기존 값을 그대로 사용한다.
- `document-ingest`에서 실제 적재 파일을 생성하거나 `build-load-files`를 실행하면 임베딩을 자동 생성한다.
- 임베딩 endpoint가 없거나 응답 차원이 설정과 다르면 적재 파일 생성을 실패시킨다. 빈 벡터나 임의 fallback 벡터는 저장하지 않는다.

## 임베딩 입력

- entity: `canonical_name`과 상위 alias 20개. 전체 alias는 OpenSearch 텍스트 필드에 그대로 남아 키워드 검색에 사용한다.
- relation: `source_name relation_type_name target_name` 순서의 방향성 문장.
- 모든 벡터는 cosine 검색 전에 단위 벡터로 정규화한다.

## 환경 설정

```dotenv
EMBEDDING_BASE_URL=http://127.0.0.1:8001/v1
EMBEDDING_API_KEY=local
EMBEDDING_MODEL=BAAI/bge-m3
EMBEDDING_DIMENSION=1024
EMBEDDING_BATCH_SIZE=64
EMBEDDING_DOCUMENT_PREFIX=
EMBEDDING_QUERY_PREFIX=
EMBEDDING_NORMALIZE=true
EMBEDDING_LOCAL_ONLY=true
```

endpoint는 OpenAI 호환 `POST /v1/embeddings`를 제공해야 한다. 기본값은 loopback만 허용한다. 승인된 내부 endpoint를 사용할 때만 CLI에 `--allow-remote-embedding`을 명시한다.

## 적재 파일 생성

```powershell
woori-graph build-load-files `
  --raw-svo runs/v3_scopefree_20260820/final/raw_svo.jsonl `
  --entities runs/v3_scopefree_20260820/final/entity_dictionary.jsonl `
  --relations runs/v3_scopefree_20260820/final/relation_dictionary.jsonl `
  --dictionary-version v3-scopefree-20260820 `
  --age-graph-name svo `
  --env-file .env `
  --output runtime/load/v3_scopefree_20260820 `
  --overwrite
```

이 명령은 OpenSearch index 설정에 `index.knn=true`를 넣고 두 index에 Lucene HNSW·cosine `knn_vector` 필드를 만든다. `--without-embeddings`는 진단·이전 호환용이며 표준 적재에서는 사용하지 않는다.

벡터 차원이나 모델을 변경하면 기존 index를 제자리 수정하지 않고 다음 명령으로 OpenSearch만 교체한다.

```powershell
.\deploy\local-storage\load.ps1 `
  -ReleaseName v3_scopefree_20260820 `
  -ReplaceOpenSearch
```

대조 보고서는 entity/relation 전체 건수뿐 아니라 `embedding` 필드가 존재하는 문서 수도 전체 건수와 같은지 검사한다.

## 신규 문서 자동 처리

`config/document_ingest.example.toml`에서 `storage.build_load_files=true`이면 `document-ingest`가 같은 환경 설정을 읽어 신규 entity와 relation 벡터를 자동 생성한다. 임베딩 설정이 없으면 vector 없는 OpenSearch 파일을 만들지 않고 실행을 중단한다.
