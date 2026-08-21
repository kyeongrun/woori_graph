# 파이프라인 구현 현황

## 1. 사전 구축

| 단계 | 구현 | 재사용 진입점 | 비고 |
|---|---|---|---|
| Markdown 의미 단위 분할 | 완료 | `documents.segment_paths`, `segment_text`, `DictionaryBuildPipeline.segment_*` | 파일·디렉터리와 향후 API 문자열 입력 모두 지원 |
| 말단 호·목 문맥 완결화 | 구현·전수 실행 중 | `context_resolution`, `resolve-context`, `audit-context` | 원문과 governing/resolved text를 분리하고 일반 문단은 verbatim 복사 |
| raw SVO 추출 | 완료 | `extraction.extract_units`, `DictionaryBuildPipeline.extract` | 운영 프롬프트는 `prompts/raw_svo_extract.ko.md`가 기준 |
| entity/relation 후보 생성 | 완료 | `candidates.build_candidate_dictionaries`, `candidates` CLI | 원표현 전수 목록 생성 |
| entity name 정규화 | 완료 | `entity_clustering`, `contextual_entities`, 관련 CLI | 결정적 규칙·LLM mapping·override를 단계별 재실행 가능 |
| entity type 부여 | 완료 | `entity_typing`, `classify-entity-types` CLI | 모든 canonical entity를 LLM으로 5종 분류, 전수 LLM provenance와 타입 누락 release 적재 차단 |
| relation type 정규화 | 완료 | `normalization`, `closed_relations`, 관련 CLI | 98개 폐쇄형 관계 사전 및 강제 mapping 지원 |
| 자동 release 승인·이력 | 미구현 | 해당 없음 | 사용자가 현재 범위에서 제외한 기능 |

사전 구축은 장시간 LLM 작업과 중간 mapping 재사용이 필요하므로 작은 단계 CLI로 유지한다. 하나의 자동 승인 명령으로 합치지 않는다.

## 2. 신규 문서 그래프 구축

| 단계 | 구현 | 재사용 진입점 |
|---|---|---|
| 문서 분할 | 완료 | `GraphBuildPipeline.segment_path`, `segment_document` |
| raw SVO 추출 | 완료 | `GraphBuildPipeline.extract` |
| entity 사전 mapping 및 신규 원문명 fallback | 완료 | `graph_mapping.map_raw_svo_to_graph` |
| entity type 상속·신규 분류 | 완료 | `entity_typing`, `graph_mapping.map_raw_svo_to_graph` |
| relation 폐쇄 사전 강제 mapping | 완료 | `graph_build.relation_mapper` |
| 표준 work/final JSONL 및 manifest | 완료 | `graph_build.ingest.run_document_ingest` |
| 설정 기반 전체 실행 | 완료 | `woori-graph document-ingest --config <toml>` |
| RDB·AGE·OpenSearch 적재 파일 | 완료 | `storage.build_storage_load_files` |
| OpenSearch entity/relation 임베딩 자동 생성 | 완료 | `embeddings`, `storage.build_storage_load_files` |
| 하이브리드 후보 검색·최대 3-hop 탐색 | 완료 | `search.GraphSearchPipeline` |
| 문서 근거 답변·독립 HTML 그래프 | 완료 | `search.artifacts` |
| 문서 간 질문 10개 발견·재검증 | 완료 | `search.cross_document` |
| 질문형 웹 화면·탐색 진단 | 완료 | `search.web`, `search/web_assets/` |
| 로컬 Docker 적재·대조 | 완료 | `deploy/local-storage/load.ps1` |
| 내부 API router | 미구현 | 내부 통합 API 구조가 정해진 뒤 연결 |

`document-ingest` 설정의 모든 상대경로는 TOML 파일 위치를 기준으로 해석한다. 입력 파일 경로나 현재 작업 디렉터리를 UUID 구성 요소로 사용하지 않는다. API가 문서 문자열과 영구 `source_document_key`를 제공하면 `segment_document`부터 같은 추출·mapping 함수를 호출하면 된다.

## 3. 저장소 이름과 책임

- PostgreSQL DB: `graphdb`
- RDB schema: `graph`
- AGE graph: `svo`
- OpenSearch 고정 alias: `entities`, `relations`
- 사전 버전은 저장소 이름이나 DB 컬럼이 아니라 JSON manifest에 기록
- `dictionary_match`, `dictionary_version`, `mention_count`, `load_release` 테이블은 적재 모델에서 제외
- OpenSearch에만 `embedding` 벡터를 저장하며 RDB·AGE에는 복제하지 않음
- entity/relation UUID 문자열은 PostgreSQL, AGE, OpenSearch에서 동일하게 사용
- RDB와 OpenSearch는 `entity_type` 필드에 타입을 저장하고, AGE는 같은 타입을 5개 vertex label로 표현
- AGE vertex 속성은 `id`, edge 속성은 `id`, `source_name`, `target_name`만 저장
