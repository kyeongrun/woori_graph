# woori_graph

법령과 회사 내부규정에서 근거가 재현되는 SVO를 추출하고, 엔티티·관계 사전으로 정규화해 그래프 탐색에 사용하는 프로젝트다.

현재 기준 설계는 [v3 SVO 그래프 설계 지시서](docs/v3-svo-graph-design-instruction.ko.md)다. 이 저장소는 v2 구현을 복제하지 않는다.

## 현재 포함 범위

- 법령 원문 입력: `data/raw/`
- Qwen/OpenAI 호환 LLM 환경 예시: `.env.example`
- v3 설계 지시서: `docs/`
- 저장소 전체에서 재사용할 안정 ID 유틸리티: `src/woori_graph/ids.py`

## 두 파이프라인

소스 책임은 두 업무로 구분한다.

1. `woori_graph.dictionary_build`: 문서 분할 → raw SVO 추출 → entity name/relation type 정규화 사전 생성
2. `woori_graph.graph_build`: 신규 문서 분할 → raw SVO 추출 → 확정 사전 매핑 → 저장소 중립 적재 레코드 생성

그래프 구축에서 엔티티가 사전에 매핑되면 확정 ID와 canonical name을 사용하고 원표현을 aliases에 포함한다. 매핑되지 않으면 `_`를 붙이지 않고 원문 이름 그대로 안정 ID를 만들어 적재한다. 관계는 반드시 확정 관계 타입에 매핑하며 자유 형식 관계 fallback은 허용하지 않는다. 실제 API 라우터와 RDB·AGE·OpenSearch 어댑터는 내부 프로젝트 구조가 정해진 뒤 내부망에서 연결한다.

세부 모듈과 내부 연결 지점은 [폐쇄망 이관용 소스 구조](docs/closed-network-source-structure.ko.md)에 정리했다.

관계는 대표 행위의 긍정·부정으로만 정규화한다. 의무·허용·금지 등의 이전 규범 분류는 운영 관계 타입으로 사용하지 않는다.

회사 내부규정은 `data/company_raw/`에만 두며 Git에 추가하거나 외부 LLM으로 전송하지 않는다.

## 현재 구현: 사전 구축 JSONL

개발 환경을 설치한 뒤 아래 순서로 실행한다. 추출기는 기본값으로 loopback vLLM만 허용한다.

```powershell
py -3 -m pip install -e ".[dev]"

woori-graph segment `
  --input data/raw `
  --output data/build/semantic_units.jsonl

woori-graph extract `
  --units data/build/semantic_units.jsonl `
  --output data/build/raw_svo.jsonl `
  --env-file .env `
  --batch-size 200

woori-graph candidates `
  --raw-svo data/build/raw_svo.jsonl `
  --entities-output data/build/entity_candidates.jsonl `
  --relations-output data/build/relation_candidates.jsonl

woori-graph normalize `
  --raw-svo data/build/raw_svo.jsonl `
  --entities-output data/build/entity_dictionary.initial.jsonl `
  --relations-output data/build/relation_dictionary.initial.jsonl `
  --edges-output data/build/normalized_edges.initial.jsonl `
  --relation-map-output data/build/relation_alias_map.initial.jsonl `
  --errors-output data/build/relation_normalization.errors.jsonl `
  --env-file .env

woori-graph compress-relations `
  --relations-input data/build/relation_dictionary.initial.jsonl `
  --taxonomy-input data/taxonomy/relation_taxonomy.closed-100.seed.jsonl `
  --taxonomy-output data/build/relation_taxonomy.jsonl `
  --map-output data/build/relation_closed_map.jsonl `
  --dictionary-output data/build/relation_dictionary.closed.jsonl `
  --audit-output data/build/relation_closed.audit.json `
  --errors-output data/build/relation_closed.errors.jsonl `
  --env-file .env

woori-graph renormalize-entities `
  --entities-input data/build/entity_dictionary.initial.jsonl `
  --raw-svo data/build/raw_svo.jsonl `
  --map-output data/build/entity_contextual_map.jsonl `
  --dictionary-output data/build/entity_dictionary.contextual.jsonl `
  --audit-output data/build/entity_contextual.audit.json `
  --errors-output data/build/entity_contextual.errors.jsonl `
  --candidates-only `
  --env-file .env
```

기존 검토 JSONL을 덮어쓰려면 각 명령에 `--overwrite`를 명시해야 한다. 장시간 추출이 중단되면 같은 명령에 `--resume`을 추가해 이미 완료한 `semantic_unit_id`를 건너뛴다. 설정된 LLM이 loopback이 아니라 명시적으로 승인한 private endpoint라면 `--allow-remote-llm`도 지정한다.

이미 생성했거나 사람이 수정한 관계 맵을 재사용할 때는 `normalize --relation-map-input <JSONL>`을 추가한다. 이 경우 LLM을 다시 호출하지 않고 그 맵으로 엔티티·관계·엣지를 재생성한다. `--retry-fallback`을 같이 주면 fallback 항목만 LLM에 재요청한다.

최종 사전 재구축에는 다음 보조 명령도 사용한다.

- `sanitize-svo --units <semantic_units.jsonl>`: 병렬 shard를 최신 조건·열거 규칙으로 다시 정제하고 원본 의미 단위 순서·전수 커버리지를 검증한다.
- `seed-entity-map`: 이전 사전의 충돌 없는 alias만 1차 출발점으로 재사용하고, 새롭거나 모호한 이름은 fallback 상태로 남긴다.
- `cluster-entities --retry-fallback`: fallback 이름만 이름 기반 1차 정규화한다.
- `renormalize-entities`: aliases와 실제 원문을 함께 보는 2차 엔티티 정규화를 수행한다.
- `refresh-relations`: 모든 raw 술어를 기존 폐쇄형 관계 taxonomy에 강제 매핑한다.
- `audit-dictionaries`: alias 충돌, canonical-first, 안정 UUID, scope 제거, 자기참조 alias, 순수 수치 엔티티, raw 전수 매핑과 98개 관계 타입을 검사한다.

`extract`는 실패한 요청을 `<output>.errors.jsonl`에 별도 기록한다. `candidates`는 원시 문자열의 완전 일치만 묶고, `normalize`는 엔티티 원표현과 관계 원형/긍정·부정을 보수적으로 정규화한다. `이 법`, `이 영`, `이 규칙`, `이 규정`은 현재 문서명으로 해소하되 global alias로 등록하지 않는다. 그 밖에는 문서별 scope나 대명사 문맥 해소를 만들지 않으며 `위원회`, `회사` 같은 동일 엔티티명을 문서와 무관하게 하나의 canonical entity와 ID로 합친다. `compress-relations`는 모든 원천 관계를 고정 taxonomy에 매핑하며, `renormalize-entities`는 긴 조건·법령 수식어·표기 중복 후보만 문맥 LLM에 보내고 일반명사 과축약과 조문 오인을 결정적 규칙으로 차단한다. 새 구조의 운영 진입점은 기존 CLI 단계 함수를 감싼 `DictionaryBuildPipeline`과 `GraphBuildPipeline`이며 기존 CLI는 비교·재실행 호환성을 위해 유지한다.

반드시 합쳐야 하는 엔티티 표현은 `config/entity_normalization_overrides.jsonl`에 `canonical_name`을 먼저, `aliases`를 뒤에 두어 관리한다. 이 결정적 override는 LLM 제안보다 우선하므로 폐쇄망 재실행에서도 같은 이름과 UUID가 나온다.

감사의 `passed: true`는 구조·커버리지·ID·mention 보존 통과를 뜻한다. 의미 품질 경고는 참고 정보이며 약간의 숫자 조건·열거·일반명사·법령 수식어 노이즈가 남아도 결과 생성을 막지 않는다. 승인·반려·사람 확정 상태나 이력은 만들지 않는다.
