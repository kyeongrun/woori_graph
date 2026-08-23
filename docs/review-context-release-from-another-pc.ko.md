# 다른 PC에서 문맥 완결형 분할 결과 검토하기

이 문서는 다른 PC에서 저장소를 내려받아 `v4-context-complete-20260821` 1단계 산출물을 검토하는 절차다. 커밋에는 공개 법령 60개와 문맥 완결형 semantic unit JSONL이 포함되어 있다. `.env`, 실행 로그, 회사 내부규정, PostgreSQL·AGE·OpenSearch 물리 볼륨은 포함되어 있지 않다.

## 1. 준비 사항

- Git
- Python 3.11 이상
- 약 100MB 이상의 여유 디스크 공간
- 저장소를 처음 받을 때만 네트워크 연결

JSONL 내용과 무결성만 검토할 때는 LLM 서버와 데이터베이스가 필요하지 않다. 파이프라인을 다시 실행하려면 별도로 `.env`와 loopback LLM 환경을 준비해야 한다.

## 2. 처음 받는 PC: clone

PowerShell 또는 터미널에서 다음을 실행한다.

```powershell
git clone https://github.com/kyeongrun/woori_graph.git
Set-Location woori_graph
git switch main
git pull --ff-only origin main
```

macOS/Linux에서는 `Set-Location woori_graph` 대신 `cd woori_graph`를 사용한다.

현재 검토 기준 커밋과 작업 트리를 확인한다.

```powershell
git log -1 --oneline
git status --short
```

`git status --short`에 출력이 없어야 깨끗하게 내려받은 상태다. 특정 검토 시점을 고정해야 한다면 원격 변경의 영향을 받지 않도록 아래 커밋을 직접 checkout한다.

```powershell
git checkout c120803
```

이 명령은 1단계 산출물이 처음 추가된 기준 커밋을 고정한다. 최신 검토 안내와 검증 스크립트까지 보려면 `main`을 그대로 사용한다.

## 3. 이미 clone한 PC: pull

로컬 수정이 없는지 먼저 확인한 뒤 fast-forward pull만 허용한다.

```powershell
Set-Location <저장소 경로>
git status --short
git switch main
git pull --ff-only origin main
```

`git status --short`에 로컬 변경이 표시되면 pull 전에 해당 변경을 commit하거나 별도로 보관한다. 검토용 PC에서는 로컬 파일을 덮어쓰는 `reset --hard`를 사용하지 않는다.

## 4. 자동 무결성 검증

프로젝트 패키지를 설치하지 않은 상태에서도 다음 명령을 실행할 수 있다.

```powershell
py -3 scripts/verify_context_release.py
```

macOS/Linux에서는 다음과 같다.

```bash
python3 scripts/verify_context_release.py
```

스크립트는 다음을 전부 확인하며, 하나라도 다르면 종료 코드 1을 반환한다.

- manifest에 기록된 세 파일의 SHA-256
- `source_manifest.jsonl`의 JSON 파싱, 문서 ID·경로 중복 및 60개 문서 수
- `01_semantic_units.jsonl`의 JSON 파싱, 필수 필드 및 semantic unit ID 중복
- `resolved_text` 공란 여부
- 전체 23,123개 레코드와 `COPIED` 8,230개, `CONTEXT_INHERITED` 14,893개
- 커밋된 문맥 감사 결과의 `passed: true`

정상 결과의 핵심은 다음과 같다.

```json
{
  "passed": true,
  "documents": 60,
  "semantic_units": 23123,
  "resolution_types": {
    "COPIED": 8230,
    "CONTEXT_INHERITED": 14893
  },
  "committed_audit_passed": true
}
```

## 5. 사람이 확인할 파일과 관점

검토 대상 파일은 다음과 같다.

- `artifacts/dictionary-build/v4-context-complete-20260821/source_manifest.jsonl`: 문서명, 원본 상대경로, 원문 SHA-256
- `artifacts/dictionary-build/v4-context-complete-20260821/work/01_semantic_units.jsonl`: 실제 분할·문맥 보완 결과
- `artifacts/dictionary-build/v4-context-complete-20260821/work/01_segmentation_manifest.json`: 실행 범위, 프롬프트·설정·산출물 해시
- `artifacts/dictionary-build/v4-context-complete-20260821/work/audits/01_context_resolution_audit.json`: 커버리지와 원문 보존 감사 결과
- `prompts/context_resolve.ko.md`: 문맥 보완에 사용한 프롬프트
- `config/dictionary_build.context_complete.toml`: 동시성, 프롬프트, 향후 저장소 이름 설정

`01_semantic_units.jsonl`에서 각 줄은 독립 JSON 객체다. 표본 검토 시 다음 필드를 함께 본다.

- `unit_text`: 법령에서 실제로 분할된 원문. 변경되면 안 된다.
- `governing_text`: 해당 호·목에 적용되는 상위 실행 문장.
- `resolved_text`: 다음 SVO 추출에 직접 투입할 문맥 완결형 문장.
- `resolution_type`: 원문 그대로면 `COPIED`, 상위 문맥을 결합했으면 `CONTEXT_INHERITED`.
- `context_text`, `source_ref`, `source_path`: 원문 위치를 추적하는 근거.

특히 `CONTEXT_INHERITED` 표본에서 `resolved_text`가 상위 주어·술어를 자연스럽게 계승하는지, 원문에 없던 법적 효과를 새로 만들지 않았는지 확인한다. `unit_text`와 위치 필드는 원문 대조용이므로 `resolved_text`와 다르다는 이유만으로 오류는 아니다.

PowerShell에서 표본 5개를 읽는 예시는 다음과 같다.

```powershell
Get-Content artifacts/dictionary-build/v4-context-complete-20260821/work/01_semantic_units.jsonl `
  -Encoding utf8 -TotalCount 5 |
  ForEach-Object { $_ | ConvertFrom-Json } |
  Select-Object document_title, source_ref, unit_text, governing_text, resolved_text, resolution_type |
  Format-List
```

## 6. 테스트 실행

소스 구현까지 검토하려면 가상환경을 만들고 개발 의존성을 설치한다.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest
```

macOS/Linux에서는 활성화 명령만 다음처럼 바꾼다.

```bash
source .venv/bin/activate
```

의존성 설치에는 인터넷 또는 준비된 wheelhouse가 필요하다. JSONL 자동 검증만 할 때는 이 단계를 생략할 수 있다.

## 7. 재실행 범위와 주의사항

현재 Git 산출물은 `segment -> resolve-context`까지 완료된 1단계 결과다. 아직 `02_raw_svo.jsonl` 이후 사전 구축 결과는 이 release에 생성되지 않았다.

전체 문맥 보완을 재실행하면 23,123개 단위를 처리하므로 비용과 시간이 발생할 수 있다. `.env`는 Git에 없으며 직접 만들어야 한다. 기본 정책은 loopback LLM만 허용한다. 회사 내부규정은 `data/company_raw/`에만 두고 Git이나 외부 LLM로 보내면 안 된다.

검토 중 발견한 문제를 전달할 때는 `semantic_unit_id`, `document_title`, `source_ref`, 현재 `unit_text`·`governing_text`·`resolved_text`, 기대 결과와 오류 이유를 함께 기록하면 재현할 수 있다.
