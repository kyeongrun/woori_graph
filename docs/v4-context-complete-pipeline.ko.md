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

raw SVO 본 실행은 동시성 48, 체크포인트 200개 단위로 수행했다. 최초 JSON 형식 실패 1건은 같은 semantic unit을 출력 한도 4,096토큰으로 재요청해 복구했다. `align-svo` 명령으로 복구 파일을 원본 semantic unit 순서에 맞춰 병합했으며 endpoint sanitizer는 적용하지 않았다. 따라서 수치 조건, 긴 수식어, 일반명사 endpoint와 모델이 판단한 관계 방향을 raw noise로 그대로 보존한다.

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
