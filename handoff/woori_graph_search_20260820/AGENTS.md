# woori_graph 개발·구조 변경 지침

## 1. 목적과 적용 범위

이 문서는 `woori_graph`의 이후 개발, 리팩터링, 실행 구조 변경, 배포 패키지 작성 시의 작업 기준이다. 기존 실행 결과나 과거 설계에 소급 적용하지 않는다. 새 표준 구조에서 새 사전 버전(예: `v1`) 또는 새 수집 실행을 만들어 검증한 뒤에만 후속 단계로 진행한다.

설계 판단의 우선순위는 다음과 같다.

1. 사용자의 최신 명시 지시
2. 이 문서
3. `docs/v3-svo-graph-design-instruction.ko.md`
4. `README.md` 및 구현 코드

상위 항목과 하위 항목이 충돌하거나, 기존 산출물·사전·입력 데이터의 의미를 바꿀 수 있으면 구현 전에 영향과 선택지를 사용자에게 확인한다.

## 2. 현재 기준선

- 기능 모듈 분리는 기본적으로 유지한다. `documents`, `extraction`, `candidates`, `entity_clustering`, `normalization`, `audit`, `ids`, `jsonl`, `cli`의 책임을 불필요하게 섞지 않는다.
- 현재 강점은 의미 단위 분할, 원시 SVO 추출, 후보 생성, 일부 정규화, JSONL 감사다.
- `normalize`는 관계 매핑, 엔티티 해소, 엔티티·관계 타입·edge 생성과 출력 저장을 함께 수행한다. 이것은 점진적으로 분리할 대상이며, 새 기능을 더 추가하는 장소로 사용하지 않는다.
- 사전 구축은 분할·원시 SVO·1차 후보 생성까지 가능하지만, 최종 엔티티/릴레이션 사전 확정·버전 release·운영 매핑은 아직 완료되지 않았다.
- 신규 문서의 운영 사전 매핑과 저장소 중립 적재 레코드 생성은 `graph_build`에 구현한다. API 라우터, 실제 RDB·AGE·OpenSearch 어댑터, outbox/retry 및 저장소 간 대조는 내부 프로젝트 구조가 정해지기 전까지 구현하지 않는다.
- 회사 내부규정은 `data/company_raw/`에만 두며 Git에 추가하거나 외부 LLM으로 전송하지 않는다. 기본 LLM은 loopback 환경만 사용한다.

## 3. 변경 전 안전 규칙

- 먼저 관련 설계서, 현재 CLI, 모델/JSONL 스키마, 테스트, 기존 산출물 위치를 읽고 변경 범위를 확인한다.
- 기존 `runs/` 및 기존 `data/build/` 결과는 재현·비교의 기준 자료다. 삭제, 덮어쓰기, 이동, 형식 변경을 하지 않는다. 새 실행 ID 또는 새 사전 버전을 사용한다.
- `.env`, 비밀값, 내부규정 원문, 로그의 민감 정보는 출력·커밋·handoff 패키지에 넣지 않는다.
- 작업 트리가 이미 변경돼 있으면 관련 변경을 보존한다. 사용자의 명시 승인 없이 reset, 강제 checkout, 대량 삭제를 하지 않는다.
- 동작 방식, 공개 CLI 이름, JSONL 필드, 결정적 ID 규칙을 변경할 때는 호환성 영향과 마이그레이션/재생성 방법을 함께 제공한다.

## 4. 목표 아키텍처와 단계 경계

새 표준 파이프라인은 아래의 작은 재실행 가능 단계로 만든다. 각 하위 명령은 입력과 출력을 명시하고, 상위 명령은 이 단계를 조합만 한다.

```text
segment
  → extract-svo
  → build-candidates
  → build-entity-map
  → build-relation-map
  → build-dictionaries
  → map-svo
  → build-load-files
  → audit
  → load
  → reconcile
```

사용자용 진입점은 다음 두 개를 목표로 한다.

```text
woori-graph dictionary-build --config <config.toml>
woori-graph document-ingest --config <config.toml>
```

- 순수 변환 로직은 모듈 함수로 두고, CLI는 인자 검증·경로 해석·단계 조합만 담당한다.
- 한 단계는 이전 단계의 명시 산출물만 입력으로 사용한다. 사전을 고쳤을 때 `map-svo`부터 같은 공식 경로로 재생성할 수 있어야 한다.
- 기존 `extract`, `candidates`, `normalize`, `cluster-entities` CLI는 호환성 보존이 필요한 동안 유지한다. 제거·이름 변경은 대체 명령과 마이그레이션 안내가 준비된 뒤에만 한다.
- `load`와 `reconcile`은 스키마, 대상 저장소, upsert/idempotency, 실패 복구 방안이 명시된 작업에서만 추가한다.

## 5. 설정과 경로 규칙

- 실행 당시 현재 작업 디렉터리에 의존하는 상대경로를 새 코드에 추가하지 않는다.
- 새 상위 명령은 `--config`를 요구하거나 안전한 기본 설정 파일을 사용한다. 필요하면 `--workspace-root`를 제공한다.
- 설정 안의 모든 상대경로는 **설정 파일이 있는 디렉터리**를 기준으로 해석한다. 예: `source_root`, `artifact_root`, `prompt_root`.
- 코드 안에 로컬 절대경로를 넣지 않는다. 실행 위치에 따라 `data/`, `prompts/`, `.env` 탐색 결과가 바뀌지 않아야 한다.
- 문서 ID의 기준은 영구적인 `source_document_key` 또는 공식 법령 ID다. 실제 파일 경로는 출처 메타데이터로만 기록하고 ID 입력값에서 제외한다.
- 모든 입력 문서는 SHA-256과 source key, 원본 상대 경로, 수집 시각을 `source_manifest.jsonl`에 남긴다. 동일 해시 재수집의 처리 정책은 명시적으로 결정한다.
- UUID는 현재 `ids.py`의 결정적 UUIDv5 원칙을 유지한다. 사전/정규화 결과에서 한 번 정한 UUID를 저장소별로 다시 발급하지 않는다.

## 6. 프롬프트 관리 규칙

- 프롬프트 파일이 유일한 기준(source of truth)이다. Python 코드에 운영 프롬프트 본문을 중복 보관하지 않는다.
- 표준 파일명은 역할 중심으로 한다: `raw_svo_extract.ko.md`, `entity_normalize.ko.md`, `relation_classify.ko.md`, `context_resolve.ko.md`.
- 기본 프롬프트와 `--prompt-file` 실행의 결과 기준이 달라져서는 안 된다. 기본값도 같은 프롬프트 파일을 가리킨다.
- 각 실행 manifest에는 사용한 프롬프트의 상대 경로, SHA-256, 적용 시각을 기록한다.
- 프롬프트 변경은 사전·추출 결과의 재현성에 영향을 주므로, 기존 release의 manifest를 수정하지 않고 새 실행/새 release로 남긴다.

## 7. 산출물·사전 release 규칙

중간 상태는 파일명 접미사(`initial`, `corrected`, `retry`, `tmp` 등)가 아니라 `work/`와 manifest에서 표현한다. 최종 사용자가 보는 `final/`에는 표준 파일명만 둔다.

```text
artifacts/
├─ dictionary-build/<dictionary_version>/
│  ├─ work/
│  │  ├─ 01_semantic_units.jsonl
│  │  ├─ 02_raw_svo.jsonl
│  │  ├─ 03_entity_candidates.jsonl
│  │  ├─ 04_relation_candidates.jsonl
│  │  ├─ 05_entity_alias_map.jsonl
│  │  ├─ 06_relation_alias_map.jsonl
│  │  └─ logs/
│  └─ final/
│     ├─ entity_dictionary.jsonl
│     ├─ relation_dictionary.jsonl
│     ├─ dictionary_manifest.json
│     └─ dictionary_audit.json
└─ document-ingest/<ingestion_run_id>/
   ├─ work/
   │  ├─ 01_semantic_units.jsonl
   │  ├─ 02_raw_svo.jsonl
   │  ├─ 03_entity_mapping_results.jsonl
   │  └─ 04_relation_mapping_results.jsonl
   └─ final/
      ├─ documents.jsonl
      ├─ entities.jsonl
      ├─ relation_types.jsonl
      ├─ relations.jsonl
      ├─ unmapped_entity_candidates.jsonl
      ├─ ingestion_manifest.json
      └─ load_audit.json
```

- `final/`은 검증된 불변 release로 취급한다. 수정이 필요하면 새 dictionary version 또는 새 ingestion run을 생성한다.
- manifest에는 입력 문서 manifest/해시, 설정 파일 및 해시, 프롬프트 및 해시, 프로그램 버전/커밋 식별자, 명령 인자, 생성 시각, 산출물별 해시와 레코드 수를 담는다.
- 관계 타입 사전은 `relation_types` 또는 `relation_dictionary`, 실제 SVO edge/인스턴스는 `relations` 또는 `relation_instances`로 구분한다. `normalized_edges.jsonl`은 새 표준 final 이름으로 사용하지 않는다.
- 사전 version은 문서·매핑 결과·적재 레코드에 함께 기록한다. 기존 release의 version을 재사용해 내용만 교체하지 않는다.

## 8. 데이터·정규화 규칙

- 원시 SVO와 근거(`document_id`, 조문 위치, `semantic_unit_id`, 원표현, 문맥)는 정규화 후에도 보존한다.
- 사전 밖 엔티티는 `_`나 `PROVISIONAL` 접미사 없이 원표현을 canonical name으로 사용해 적재 레코드를 만든다. 사전 alias에 매핑되면 사전 ID를 사용하고 해당 실행의 aliases에 원표현을 포함한다.
- 엔티티에는 일반 문서·조문 scope를 두지 않는다. 단, `이 법`, `이 영`, `이 규칙`, `이 규정`은 현재 문서명으로 결정적으로 해소하되 global alias로 등록하지 않는다. 위원회·회사·담당자 같은 나머지 일반 표현은 문맥의 정식 명칭으로 해소하지 않고 동일 원문 이름을 전역 통합한다.
- 엔티티 타입은 설계서의 다섯 값(`ORGANIZATION`, `PERSON`, `LEGAL_INSTRUMENT`, `CONCEPT`, `OTHER`)만 사용한다. 타입은 entity ID의 구성 요소가 아니다.
- 릴레이션은 대표 행위의 긍정/부정으로만 타입을 구분한다. 의무·허용·금지를 별도 운영 relation type으로 새로 만들지 않으며, 신규 술어도 반드시 확정 사전 타입에 강제 매핑한 뒤에만 적재 레코드를 만든다.
- 새 문서의 운영 매핑은 확정 사전 version을 명시적으로 입력으로 받아야 하며, 그 version과 매핑 근거를 결과에 기록한다.

## 9. 품질 검증과 테스트

- 코드 변경은 관련 단위 테스트를 추가·수정하고 실행한다. 기존 테스트의 기대값을 이유 없이 완화하지 않는다.
- 단계 분리·설정 도입 시에는 최소한 다음 통합 테스트를 추가한다: 다른 현재 디렉터리에서 config 기반 실행, config 상대경로 해석, 동일 문서의 안정 ID, manifest 해시 기록, 사전 변경 후 `map-svo` 재실행.
- 적재기가 도입되면 RDB/AGE/OpenSearch용 독립 통합 테스트를 추가한다. 동일 실행의 재시도 idempotency, 부분 실패 후 복구, 세 저장소의 ID 대조를 검증한다.
- 감사는 커버리지, UUID 형식/중복, 참조 무결성, 입력·출력 레코드 수, 미매핑·fallback·모호 항목을 보고해야 한다. 심각한 무결성 오류가 있으면 `final/` release를 만들지 않는다.
- 장시간 LLM 실행 전에는 작은 표본에서 분할 품질, 재시도, 지연, 프롬프트 버전을 확인한다.

## 10. 폐쇄망 handoff 및 의존성

- 전달은 저장소 전체 압축이 아니라 재현 가능한 전용 `handoff/` 패키지로 한다.
- handoff에는 application 소스·테스트·프롬프트·config·문서·`pyproject.toml`, 허용된 bootstrap 문서와 manifest, 확정 dictionary release만 포함한다.
- `.env`, `.git`, 로그, cache, 임시·retry·pilot·shard 산출물, 외부망 전용 파일은 제외한다.
- 폐쇄망 설치가 범위에 포함될 때는 정확히 고정된 의존성 lock 파일과 해당 플랫폼용 wheelhouse를 함께 만들고, 설치·검증 절차를 문서화한다. `pyproject.toml`의 범위 제약만으로는 충분하지 않다.

## 11. 작업 완료 기준

구조 변경은 다음을 모두 만족할 때 완료로 본다.

1. 변경된 책임 경계와 설정·CLI 사용법이 문서화되어 있다.
2. 기존 호환성 또는 명시적인 마이그레이션 경로가 있다.
3. 새 산출물이 표준 `work/`/`final/` 구조와 manifest 규칙을 따른다.
4. 민감 데이터와 기존 결과물을 건드리지 않았다.
5. 관련 단위/통합 테스트와 감사가 통과했으며, 실행한 검증과 남은 제한 사항을 사용자에게 보고했다.
