# 폐쇄망 이관용 소스 구조

이 저장소는 내부 API 서버 자체를 구현하지 않는다. 내부 코드 어시스턴트가 기존 서버 구조에 맞춰 호출부를 연결할 수 있도록, 도메인 로직을 다음 두 업무 패키지로 구분한다.

## 1. 사전 구축

진입점: `woori_graph.dictionary_build.DictionaryBuildPipeline`

```text
문서 경로 또는 문서 문자열
  → segment_path / segment_document
  → extract
  → normalize
  → entity_dictionary / relation_dictionary
```

단계별 실제 구현은 다음 모듈을 재사용한다.

| 단계 | 모듈 | 책임 |
|---|---|---|
| 문서 분할 | `documents.py` | 조·항·호·목과 상위 문맥을 의미 단위로 구성 |
| 원시 SVO 추출 | `extraction.py` | 핵심 명사의 수량 조건·금액 제거, 열거 분리, 사람 자격 상태 표현과 근거 위치 보존 |
| 원표현 후보 | `candidates.py` | raw entity/relation 목록 생성 |
| 엔티티 1차 정규화 | `entity_clustering.py` | 전체 표면형의 canonical name 제안, 동일 이름 전역 통합, 결정적 override 적용 |
| 엔티티 2차 정규화 | `contextual_entities.py` | 1차 aliases와 실제 원문 문맥으로 대표명 재통합 |
| 관계 정규화 | `normalization.py`, `closed_relations.py` | 긍정/부정 관계 원형화와 100개 이하 폐쇄형 사전 생성 |

## 2. 그래프 구축

진입점: `woori_graph.graph_build.GraphBuildPipeline`

```text
신규 문서 경로 또는 문서 문자열
  → segment_path / segment_document
  → extract
  → map_for_load
  → documents / entities / relation_types / relations 레코드
```

- 엔티티가 확정 사전의 canonical name 또는 alias와 일치하면 사전의 `entity_id`와 canonical name을 사용한다. 신규 원표현은 `_`를 붙이지 않고 그 이름 그대로 안정 UUID를 생성해 적재 레코드에 포함한다.
- `이 법`, `이 영`, `이 규칙`, `이 규정`은 예외적으로 현재 문서명으로 해소한다. 원표현은 mapping result와 relation evidence에 남기고 여러 문서의 global alias로 등록하지 않는다.
- 사전 엔티티에 매핑된 신규 원표현은 해당 실행의 entity 레코드 `aliases`에 함께 포함한다. 확정 사전 파일 자체를 실행 중 수정하지 않는다.
- 신규 문서의 그래프 구축에서는 사전 구축의 엔티티 1차·2차 정규화를 반복하지 않는다. 사전에 없는 엔티티는 원표현 그대로 적재한다.
- 관계는 자유 형식 fallback을 허용하지 않는다. 사전 alias에 없으면 `relation_classify.ko.md`로 확정 관계 타입 중 하나를 선택하며, 누락·잘못된 ID가 있으면 load record 생성을 중단한다.
- 동일 `source entity + relation type + target entity`는 하나의 `relation_id`로 통합하고 근거를 `evidence`에 누적한다.
- `GraphLoadBundle`은 저장소 중립 레코드다. 내부 프로젝트가 RDB·AGE·OpenSearch 어댑터를 붙이되, 번들에 들어 있는 UUID 문자열을 저장소별로 다시 만들면 안 된다.

## 공통 기반

```text
src/woori_graph/
├─ dictionary_build/       # 사전 구축 업무 진입점
├─ graph_build/            # 그래프 구축 업무 진입점과 관계 강제 매핑
├─ documents.py            # 공통 문서 분할
├─ extraction.py           # 공통 raw SVO 추출
├─ graph_mapping.py        # 사전 매핑 및 적재 레코드 생성
├─ models.py               # 단계 간 데이터 모델
├─ ids.py                  # 결정적 UUIDv5
├─ jsonl.py                # JSONL 입출력
└─ audit.py                # 구조·품질 감사
```

운영 프롬프트는 `prompts/`의 역할명 파일을 기준으로 한다. 실행 현재 디렉터리에 의존하지 않고 소스 루트 기준으로 읽으며, 내부 프로젝트가 별도 프롬프트 경로를 사용하면 호출 시 명시적으로 전달한다.

사람이 고정한 소수의 엔티티 동의 표현은 `config/entity_normalization_overrides.jsonl`에서 관리한다. 일반화는 두 정규화 프롬프트가 담당하고, override는 이미 확정한 대표명이 재실행마다 달라지는 것만 방지한다.

## 내부 프로젝트 연결 범위

내부 코드 어시스턴트가 수행할 작업은 다음으로 제한한다.

1. 내부 문서 수신 DTO를 `segment_document(content, source_document_key, title_hint)` 호출로 변환한다.
2. 내부 LLM 클라이언트를 `CompletionClient.complete(prompt) -> str` 형태로 연결한다.
3. `GraphLoadBundle`의 네 종류 레코드를 내부 RDB·AGE·OpenSearch 스키마에 맞게 upsert한다.
4. 동일 UUID, 재시도 멱등성, 부분 실패 복구와 저장소 간 ID 대조를 내부 인프라 기준으로 구현한다.

이 저장소에는 API 라우터, 인증, 내부 서버 DTO, DB 연결 문자열, 실제 적재 어댑터를 추가하지 않는다.
