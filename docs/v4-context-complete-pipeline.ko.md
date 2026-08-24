# v4 문맥 완결형 SVO 그래프 재구축

## 확정 범위

- 입력은 `data/raw/`의 현행 법령 Markdown 60개다.
- 본문만 처리하고 부칙, 별표, 개정 이력은 제외한다.
- 기존 release와 `graph`/`svo`/`entities`/`relations` 저장소는 변경하지 않는다.
- 신규 저장소는 PostgreSQL schema `graph_v2`, AGE graph `svo_v2`, OpenSearch alias `entities_v2`와 `relations_v2`를 사용한다.
- 신규 RDB에는 `semantic_unit` 테이블을 만들지 않는다. 관계 근거 레코드에 `semantic_unit_id`와 `evidence`를 저장한다.
- AGE vertex에는 `id`만, edge에는 `id`, `source_name`, `target_name`만 저장한다.

## 파이프라인

```text
segment
  -> resolve-context
  -> extract-svo
  -> build-candidates
  -> build-entity-map
  -> build-relation-map
  -> build-dictionaries
  -> map-svo
  -> build-load-files
  -> audit
  -> load
  -> reconcile
  -> compare-search
```

`segment`는 원문 `unit_text`와 상위 실행 문장 `governing_text`를 분리한다. `resolve-context`는 말단 호·목마다 두 표현을 결합한 `resolved_text`를 만들며 원문 필드는 수정하지 않는다. 이미 완결된 일반 문단은 LLM을 호출하지 않고 `COPIED`로 기록한다.

`extract-svo`는 `resolved_text`를 주 입력으로 사용하고 `unit_text`, `governing_text`, `context_text`를 근거 확인에 사용한다. raw SVO endpoint는 LLM 출력을 그대로 보존한다.

## 단계별 진행 상태

- `segment -> resolve-context`: 23,123개 semantic unit 완료, 감사 통과
- `extract-svo`: 23,123개 레코드와 29,818개 raw 관계 완료, 감사 통과
- `build-candidates`: 출처 포함 exact surface 엔티티 25,307개와 관계 6,220개 완료, 감사 통과
- `build-entity-map`: 2단계 이름 정규화와 다섯 타입 분류 완료, 감사 통과
- `build-relation-map`: raw 술어 6,220개를 폐쇄형 관계 타입 100개에 매핑 완료, 감사 통과

raw SVO 본 실행은 동시성 48, 체크포인트 200개 단위로 수행했다. 최초 JSON 형식 실패 1건은 같은 semantic unit을 출력 한도 4,096토큰으로 재요청해 복구했다. `align-svo` 명령으로 복구 파일을 원본 semantic unit 순서에 맞춰 병합했으며 endpoint sanitizer는 적용하지 않았다. 따라서 수치 조건, 긴 수식어, 일반명사 endpoint와 모델이 판단한 관계 방향을 raw noise로 그대로 보존한다.

후보 목록의 `source_text`는 실제 추출 입력인 `resolved_text`를 우선 사용하고, 문맥 완결문이 없을 때만 `unit_text`를 사용한다. 각 행에는 `semantic_unit_id`, `document_title`, `source_ref`, 최초 원문 근거와 전체 출현 빈도가 함께 저장된다. 이 단계에서는 문자열 완전 일치만 묶고 의미 군집화는 적용하지 않는다.

엔티티 이름은 과거 수동 override 없이 LLM으로 두 번 정규화했다. 1차는 25,307개 raw 표면형을 이름 중심으로 19,231개에 군집화했고, 2차는 aliases와 실제 원문 근거를 함께 보아 18,038개 canonical entity로 군집화했다. 2차는 모든 source entity가 LLM mapping을 가져야 하는 엄격 모드로 실행했다. 최종 typed dictionary에서 raw alias와 중간 canonical alias 37,851개를 최종 entity ID로 직접 연결했다.

엔티티 타입도 규칙 선분류 없이 LLM이 전수 판정했다. 사용 값은 `ORGANIZATION`, `PERSON`, `LEGAL_INSTRUMENT`, `CONCEPT`, `OTHER`뿐이다. 최종 분포는 `CONCEPT` 13,057개, `PERSON` 2,246개, `ORGANIZATION` 1,250개, `LEGAL_INSTRUMENT` 931개, `OTHER` 554개다. 엔티티 release 감사에서 raw 후보 전수 포함, alias 단일 귀속, UUID 참조, 타입 폐쇄성, mention 59,636개 보존을 확인했다.

관계는 raw 술어 6,220개를 LLM으로 긍정·부정 1차 행위 타입 1,335개에 정규화한 뒤, 같은 60개 법령 corpus에서 확정한 50개 행위군 seed taxonomy의 긍정·부정 100개 타입에 전수 재매핑했다. 새 장문 taxonomy 생성은 반복된 timeout·잘린 JSON 때문에 사용하지 않았고, 각 새 술어의 최종 family 선택은 이번 실행의 LLM이 다시 판단했다. 최종 직접 alias map은 관측 raw 술어 6,220개만 포함하며 합성된 0회 canonical alias는 관계 사전에만 유지한다. relation mention 29,818개가 보존됐다.

## 표준 산출물

```text
artifacts/dictionary-build/v4-context-complete-20260821/
├─ source_manifest.jsonl
├─ work/
│  ├─ 01_semantic_units.jsonl
│  ├─ 02_raw_svo.jsonl
│  ├─ 03_entity_candidates.jsonl
│  ├─ 04_relation_candidates.jsonl
│  ├─ 05_entity_alias_map.jsonl
│  ├─ 06_relation_alias_map.jsonl
│  ├─ 07_mapped_svo.jsonl
│  ├─ audits/
│  └─ load_files/
└─ final/
   ├─ entity_dictionary.jsonl
   ├─ relation_dictionary.jsonl
   ├─ dictionary_manifest.json
   └─ dictionary_audit.json
```

`runs/`의 shard와 재시도 파일은 실행 중간 자료다. Git에는 표준 JSONL, 소스, 프롬프트, 테스트와 manifest를 포함하고 로그와 실제 PostgreSQL/OpenSearch 데이터 볼륨은 포함하지 않는다.

## 엔티티 정규화

- 타입은 `ORGANIZATION`, `PERSON`, `LEGAL_INSTRUMENT`, `CONCEPT`, `OTHER` 다섯 값만 사용한다.
- 일반 표현인 `위원회`, `회사`, `담당자` 등을 문맥의 특정 정식 명칭으로 확대 해석하지 않는다.
- `이 법`, `이 영`, `이 규칙`, `이 규정`만 현재 문서명으로 결정적으로 해소하고 global alias로 등록하지 않는다.
- entity ID는 canonical name의 결정적 UUID이며 타입은 ID 입력값이 아니다.

## 관계 정규화

- 전체 원천 술어를 100개 이하의 폐쇄형 관계 타입에 매핑한다.
- 의무, 허용 같은 modality는 별도 AGE 속성이나 관계 타입을 만들지 않고 대표 행위로 묶는다.
- 긍정과 부정은 별도 canonical relation type으로 만든다. 예: `제출하다`, `제출하지 않다`.
- 능동/피동은 의미상 행위 방향이 같으면 능동 대표형으로 묶되, 주체와 대상 방향이 바뀌는 표현은 합치지 않는다.

## 비교 화면

기존/신규 비교의 중심은 총 레코드 수가 아니라 동일 질의의 탐색 동작이다.

- OpenSearch 엔티티·관계 후보와 점수
- 선택된 seed
- AGE 탐색 경로와 hop
- RDB 원문 근거
- 최종 근거 답변
- 단계별 지연과 실패 진단

같은 질문을 기존 `graph`/`svo`/기존 alias와 신규 `graph_v2`/`svo_v2`/`*_v2`에 동시에 실행하여 좌우로 비교한다.
