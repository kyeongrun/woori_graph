# v3 SVO 초기 추출·정규화 handoff

> 이 문서는 2026-08-19 실행 당시의 역사 기록이다. 이후 설계에서는 문서별 scope와 일반 역할명의 문맥 해소를 제거했으므로 현재 정책은 `docs/v3-svo-graph-design-instruction.ko.md`와 최신 run의 HANDOFF를 따른다.

작업일: 2026-08-19  
기준 설계: `docs/v3-svo-graph-design-instruction.ko.md`  
실행 결과: `runs/v3_initial_20260819/`

## 1. 완료 범위

60개 현행 법령 Markdown의 본문을 대상으로 다음 작업을 완료했다.

1. SVO 추출 요청 단위로 문서 분할
2. 모든 분할 레코드에 대한 원시 SVO 추출
3. 원표현 완전 일치 후보 사전 생성
4. 자기참조·문서 범위 alias의 보수적 엔티티 해소
5. 관계명의 용언·활용·긍정/부정 1차 정규화
6. 동일 `source entity + relation type + target entity`의 edge 통합과 근거 누적
7. UUID 형식, 유일성, 참조 무결성, 추출→edge 근거 전수 커버리지 감사

현재 단계는 JSONL 사전 구축·검토 단계이다. RDB, Apache AGE, OpenSearch에는 아무것도 적재하지 않았다. `부칙`과 개정 이력도 이번 대상에서 제외했다.

## 2. 의미 단위 정의와 분할 결과

이 작업에서 `의미 단위`는 명사나 명사구가 아니라 **LLM에 한 번 전달하는 SVO 추출 요청 단위**다.

예를 들어 상위 문장이 `위원회는 다음 각 호의 업무를 처리한다`이고 말단 호가 `신청서 접수`라면 다음과 같이 처리한다.

- `context_text`: 상위 조항·호와 `위원회는 … 처리한다`
- `unit_text`: `신청서 접수`
- 추출 목표: `위원회 / 처리하다 / 신청서 접수`
- 생성하지 않는 관계: `위원회 / 처리하다 / 각 호`

상위 주어·술어와 말단 명사구는 `context_text + unit_text` 하나의 추출 요청으로 전달된다. 상위 `각 호`문을 별도 SVO 요청으로 만들지 않는 회귀 테스트도 추가했다.

분할 결과는 23,123건이며 모두 고유한 `semantic_unit_id`를 갖는다.

| 구분 | 건수 |
|---|---:|
| 단독 항/조문 본문 `paragraph` | 7,874 |
| 말단 호·목 `terminal_item` | 15,249 |
| 합계 | 23,123 |

가장 긴 `context_text + unit_text`는 1,289자였고 4,000자를 넘는 레코드는 없다.

## 3. 최종 결과 요약

| 지표 | 결과 |
|---|---:|
| 입력 법령 | 60개 |
| SVO 요청 단위 | 23,123 |
| 완료된 원시 SVO 레코드 | 23,123 |
| 원시 relation mention | 22,711 |
| 명시적 SVO가 없는 레코드 | 7,572 |
| 원표현 엔티티 후보 | 18,787 |
| 원표현 관계 후보 | 3,052 |
| 1차 엔티티 사전 | 18,700 |
| 1차 관계 타입 사전 | 1,623 |
| 고유 normalized edge | 20,219 |
| edge에 누적된 근거 | 22,711 |

20,219개 edge 중 19,114개는 근거 1개, 1,105개는 근거 2개 이상을 갖는다. 따라서 동일 triple은 edge를 중복 생성하지 않고 `evidence` 배열에 출처를 누적했다.

관계 원표현 3,052개 중 3,051개는 LLM이 제안한 canonical_name과 polarity가 형식 검증을 통과했다. `삭제하거나 식별할 수 없도록 하여야 한다` 1개는 복합 술어이며 극성과 canonical_name이 충돌해 `fallback_raw_invalid_proposal`로 원표현을 보존했다. LLM 관계 정규화 배치 호출 실패는 0건이다.

## 4. 주요 산출물

| 파일 | 용도 |
|---|---|
| `runs/v3_initial_20260819/semantic_units.jsonl` | 출처 위치와 상위 문맥을 보존한 SVO 추출 요청 단위 |
| `runs/v3_initial_20260819/raw_svo.complete.jsonl` | 23,123건 전수 추출 결과 |
| `runs/v3_initial_20260819/entity_candidates.exact.jsonl` | 원표현 완전 일치 엔티티 후보 |
| `runs/v3_initial_20260819/relation_candidates.exact.jsonl` | 원표현 완전 일치 관계 후보 |
| `runs/v3_initial_20260819/entity_dictionary.initial.jsonl` | 자기참조·문서 범위 alias를 반영한 1차 엔티티 사전 |
| `runs/v3_initial_20260819/relation_alias_map.initial.jsonl` | 원표현 관계→canonical_name/polarity 검토 맵 |
| `runs/v3_initial_20260819/relation_dictionary.initial.jsonl` | canonical_name 기준 1차 관계 타입 사전 |
| `runs/v3_initial_20260819/normalized_edges.initial.jsonl` | 고유 triple과 누적 근거 |
| `runs/v3_initial_20260819/audit.json` | 최종 커버리지·ID·참조 감사 결과 |

`raw_svo.errors.jsonl`의 43줄은 shard 추출과 재시도 과정의 **역사 로그**다. 현재 미처리 오류 목록이 아니며, 최종 커버리지는 `raw_svo.complete.jsonl`과 `audit.json`을 기준으로 판단한다.

## 5. ID 및 정합성

엔티티, 관계 타입, edge, 문서, 의미 단위, 관계 mention ID는 안정적 UUIDv5 문자열로 생성한다. 이후 적재 단계에서는 JSONL에서 확정한 **동일한 소문자 hyphenated UUID 문자열**을 다음과 같이 그대로 사용해야 한다.

- RDB entity/relation PK
- AGE vertex/edge `id`
- OpenSearch document `_id` 및 `entity_id`/`relation_id`

이번 감사에서 15개 검사 항목이 모두 통과했다.

- 의미 단위, 원시 SVO, relation mention, entity, relation type, edge ID 유일성
- entity/relation type/edge ID의 UUID 문자열 형식
- 모든 SVO 요청 단위의 추출 커버리지
- 모든 edge의 source/target entity와 relation type 참조 존재
- 각 edge의 `evidence_count`와 실제 `evidence` 개수 일치
- 22,711개 relation mention과 edge evidence ID 집합의 전수 일치 및 중복 0건

현재는 DB 적재 전이므로 세 저장소 간 실물 교차 검증은 아직 할 수 없다. 적재기를 구현할 때는 ID를 재생성하지 않고 이 JSONL ID를 재사용하며, 적재 후 세 저장소의 ID 집합을 교차 검사해야 한다.

## 6. 예외 처리

`manual_raw_svo_overrides.jsonl`에 자본시장법 제449조제3항 제17호 1건이 있다. 해당 단위는 조건 목록으로만 구성되어 외부 endpoint를 갖는 명시적 SVO가 없다고 판단했고, 반복 timeout/불완전 JSON 반환 후 `relations: []`로 명시 보존했다. 이 레코드는 내일 원문과 함께 사람이 다시 확인할 필요가 있다.

## 7. 이번에 보강한 구현

- 조·항·호·목 계층 파싱과 말단 호/목 중심 분할
- `부칙` 제외와 절 제목의 이전 조문 혼입 방지
- 추출 shard, offset/limit, 배치 즉시 저장, resume, 오류 JSONL
- 복수 주어/목적어/행위 분리, 수동문 방향, 공통 부정의 각 관계 전파, 절차 표현 제외 프롬프트
- 잘못된 relation 객체 한 개가 응답 전체를 실패시키지 않는 개별 검증
- exact candidate, 1차 사전, edge 근거 누적 생성
- LLM 관계 제안의 canonical_name/polarity 모순 차단
- 생성 관계 맵 재사용 `--relation-map-input`과 fallback만 재요청하는 `--retry-fallback`
- UUID 유일성·참조·relation mention 근거 전수 감사

자동화 테스트는 14개가 통과했다.

## 8. 내일 우선 검토·개선 사항

### 8.1 엔티티 사전

1. **인용부호·조문 포함 endpoint**: canonical_name이 `"`, `「` 등으로 시작하는 엔티티가 825개이다. 법령명·조문 참조와 실제 entity를 구분해야 한다.
2. **의미적 동의어 병합**: 현재 일반 endpoint는 원표현 완전 일치 중심이다. 조사, 복수형, 수식어 차이와 동일 기관/직위의 변형을 병합해야 한다.
3. **문서 범위 역할명**: `위원회`, `회사`, `담당자`, `담당부서` 등 미해소 역할명 30개는 문서별 ID로 격리했다. 전문·앞조항의 기관명 근거를 사용해 실제 canonical entity로 확정해야 한다.
4. **지역 alias 탐지 보강**: 현재 `가나다(이하 "다나"라 한다)`처럼 괄호 직전의 붙어 있는 이름에 유리하다. 공백·법령 인용·다중 수식어가 있는 공식명은 놓칠 수 있다.
5. **복합 `이 법` 표현**: endpoint가 `이 법`과 완전히 같을 때만 현재 문서명으로 해소한다. `이 법에 따른 …` 같은 복합 endpoint는 별도 규칙이 필요하다.

1차 엔티티 18,700개 중 global scope는 18,670개, document scope는 30개다. alias가 하나 이상 누적된 canonical entity는 181개다. 이 수치는 아직 사람이 확정한 dictionary가 아니다.

### 8.2 관계 사전과 SVO 품질

1. `relation_alias_map.initial.jsonl`의 3,052개 매핑을 빈도와 예시 출처 순으로 검토한다.
2. 하나의 복합 술어에 여러 행위가 남은 경우를 나눈다. 우선 대상은 유일한 fallback `삭제하거나 식별할 수 없도록 하여야 한다`다.
3. `하다`, `되다`, `있다` 같은 고빈도·저정보 관계명과 긴 절 형태 관계명을 우선 검토한다.
4. 수동문, 조건절, 절차절(`의결을 거쳐`, `제청으로`) 및 병렬 부정이 실제 edge 방향과 개수에 올바르게 반영되었는지 샘플링한다.
5. `공시하지 않아도 된다`처럼 미행위 허용이 관계로 생성되지 않았는지 별도로 검사한다.

### 8.3 추가 구현 필요

1. 사람이 확정한 `entity_alias_map.reviewed.jsonl` 스키마와 `normalize --entity-map-input` 재생성 경로
2. dictionary 수정 전/후의 ID 변경·병합 보고서
3. 전체가 아닌 선택 alias만 재정규화하는 엔티티/관계 리뷰 워크플로
4. 법령 링크가 제공되면 출처 URL 스키마와 document 메타데이터 보강
5. 최종 사전 승인 후에만 RDB·AGE·OpenSearch 적재기 구현

## 9. 내일 권장 작업 순서

1. `entity_candidates.exact.jsonl`과 `entity_dictionary.initial.jsonl`에서 고빈도·문서 범위·인용부호 항목을 추린다.
2. entity canonical_name과 alias를 확정할 review map 스키마를 만든다.
3. `relation_alias_map.initial.jsonl`을 복제한 reviewed map에서 1,623개 relation type을 통합·분리한다.
4. reviewed entity/relation map으로 사전과 edge를 재생성한다.
5. `audit` 재실행 후 ID 변경·edge 병합·근거 커버리지 차이를 비교한다.

관계 맵만 수정한 후 LLM 재호출 없이 사전·edge를 재생성하는 명령은 다음 형태다.

```powershell
$env:PYTHONPATH='src'
py -3 -m woori_graph normalize `
  --raw-svo runs\v3_initial_20260819\raw_svo.complete.jsonl `
  --relation-map-input runs\v3_initial_20260819\relation_alias_map.reviewed.jsonl `
  --relation-map-output runs\v3_initial_20260819\relation_alias_map.reviewed.jsonl `
  --entities-output runs\v3_initial_20260819\entity_dictionary.reviewed.jsonl `
  --relations-output runs\v3_initial_20260819\relation_dictionary.reviewed.jsonl `
  --edges-output runs\v3_initial_20260819\normalized_edges.reviewed.jsonl `
  --errors-output runs\v3_initial_20260819\relation_normalization.reviewed.errors.jsonl `
  --overwrite
```

현재 `normalize`는 reviewed entity map을 받지 않으므로 위 명령의 entity 결과는 오늘의 보수적 해소 규칙을 다시 적용한다. entity review map 지원을 먼저 구현한 다음 최종 사전을 만드는 것이 안전하다.
