# 변경 이력

이 문서는 `market-factor`의 주요 변경 사항을 기록합니다.
형식은 [Keep a Changelog](https://keepachangelog.com/ko/1.1.0/)를 따르고,
버전은 [유의적 버전](https://semver.org/lang/ko/)을 따릅니다.

날짜는 KST 기준입니다.

---

## [0.5.0] - 2026-09-04

고정 슬롯 의존을 걷어내고, 창(window) 기반으로 그날 실행을 채우도록 바꿨습니다.

### 배경

GitHub `schedule`은 실행이 보장되지 않습니다. 실제로 관측된 사례:

- 아침 스케줄이 정시(22:30 UTC)가 아니라 00:15 / 04:53 UTC에 실행 (최대 5시간 지연)
- 장마감 슬롯 3개(07:00 / 08:00 / 09:00 UTC)가 **전부 미발생**

0.4.0의 보충 슬롯도 결국 고정 시각이라, 슬롯이 한꺼번에 누락되면 소용이 없었습니다.

### 추가

- **`--mode auto`** — 현재 KST 시각으로 실행할 세션을 판별
  - 아침 창: 07:30~12:00 KST (월~토)
  - 장마감 창: 16:00~22:00 KST (월~금)
  - 창 밖이거나 그날 이미 성공했으면 아무것도 하지 않고 종료
- **`daily.yml`** — 창 안에서 매시간 시도하는 단일 디스패처
  - 슬롯 몇 개가 누락돼도 나머지 하나만 걸리면 그날 실행이 채워짐
  - 중복 방지가 내장되어 실제 수집·Gemini 호출은 세션당 1회
  - 수동 실행 시 `session` 입력으로 `auto` / `morning` / `close` 선택 가능

- **주간·월간에도 동일한 창 방식 적용**
  - 주간: 토 07:30~11:00 KST 사이 3개 슬롯 (기존 금 22:30 UTC 단일 슬롯)
  - 월간: 1일 07:30~11:00 KST 사이 슬롯 (기존 사실상 월 1회 단일 슬롯)
  - 월간은 슬롯을 놓치면 **한 달을 통째로 건너뛰는** 구조였음
- **리포트 중복 생성 방지** (`is_period_report_published`)
  - 완료 판정: `reports/YYYY-Www.html`, `reports/YYYY-MM-monthly.html` 존재 여부
  - 수동 실행 시 `force` 입력으로 강제 재생성 가능

### 제거

- `morning.yml`, `close.yml` — `daily.yml`로 통합

---

## [0.4.1] - 2026-09-04

### 수정

- **Slack 링크에 대시보드 주소 대신 안내 문구가 나오던 문제**
  - `SITE_BASE_URL` Secret이 비어 있으면 링크가 만들어지지 않았음
  - 저장소의 `CNAME`(GitHub Pages 커스텀 도메인)을 폴백으로 사용하도록 변경
  - 우선순위: `SITE_BASE_URL` → `CNAME`. 커스텀 도메인 사용 시 Secret 불필요

---

## [0.4.0] - 2026-09-04

브리핑이 조용히 누락되던 경로들을 막았습니다.

### 추가

- **놓친 스케줄 보충 슬롯**
  - 아침: 07:30 정시 + 08:30 / 09:30 보충
  - 장마감: 16:00 정시 + 17:00 / 18:00 보충
  - GitHub `schedule`은 실행이 보장되지 않아 건너뛰는 경우가 있어 추가
- **`--skip-if-done` 플래그**
  - 그날 같은 타입 실행이 이미 성공했으면 즉시 종료
  - 판정 기준: `metadata/market/YYYY/MM/DD/{type}.json`의 `status == "published"`
  - AI가 실패한 날은 metadata가 기록되지 않으므로 보충 슬롯이 자동 재시도
  - 스케줄 실행에만 적용되고, 수동 실행(`workflow_dispatch`)은 항상 강제 실행
- **원자적 파일 쓰기** (`_atomic_write`)
  - `.tmp` 기록 → `fsync` → `os.replace`로 교체
  - JSON은 교체 직전에 재파싱해 검증하므로 깨진 파일이 원본을 덮어쓰지 않음
- **프롬프트 거래시간 맥락**
  - 07:30 실행: 미국장은 간밤 마감 결과, 코스피는 개장 전(전 거래일 종가)
  - 16:00 실행: 코스피는 당일 종가, 미국장은 개장 전(직전 거래일 종가)
- **프롬프트 투자 조언 금지**
  - 매수/매도/목표주가/행동 지시 금지 명시
  - 근거가 부족하면 원인을 단정하지 않도록 지시

### 수정

- **AI 실패 시 시세 데이터까지 유실되던 문제**
  - 분석 실패로 예외가 나면 뒤따르는 커밋 스텝이 스킵되어 그날 레코드가 남지 않았음
  - 커밋 스텝에 `if: always()`를 추가해 시세는 저장하고, job은 실패로 표시
- **생성된 데이터가 커밋되지 않던 문제**
  - `git add`가 존재하지 않는 `reports` 경로에서 중단되어 아무것도 스테이징되지 않았고,
    "No data changes to commit"으로 성공 처리되고 있었음
  - 존재하는 경로만 골라 스테이징하도록 변경
- 404를 반환하는 `gemini-2.5-flash` / `gemini-2.0-flash`를 폴백 목록에서 제거
  (현재 후보: `gemini-3.8-flash` → `gemini-3.7-flash` → `gemini-3.6-flash`)

---

## [0.3.0] - 2026-09-03

Gemini 호출 안정화. 무료 등급 한도와 503(수요 폭주)에 대응했습니다.

### 추가

- **`reanalyze` 모드** — 시세 재수집 없이 그날 AI 분석만 다시 생성
  (`python lambda_function.py --mode reanalyze`, `Reanalyze Today` 워크플로)
- **응답 검증** (`_is_usable_analysis`)
  - JSON 파싱만 성공하면 "성공"으로 처리하던 것을 실제 문장 유무까지 확인하도록 변경
  - `overall` 길이와 유효 필드 수를 확인하고, 비어 있으면 다음 모델로 폴백
- **다중 파트 응답 처리** (`_extract_gemini_text`) — thinking 응답의 여러 `parts`를 합쳐서 파싱
- 분석이 비면 silent 실행(16:00)에서도 Slack 알림 발송

### 변경

- 503 / 429는 같은 모델을 재시도하지 않고 **즉시 다음 후보 모델로 전환**
  (재시도해도 같은 모델이 계속 막히는 경우가 많았음)
- `GEMINI_MODEL` 환경변수와 기본 후보 목록의 **중복 제거**
  (중복 때문에 이미 붐비는 모델을 연속 호출하고 있었음)
- HTTP 타임아웃 55초 → 90초, Actions job 타임아웃 상향
- `thinkingConfig`는 `gemini-3.x` 계열에만 전달
- `actions/checkout@v4` → `v5`, `actions/setup-python@v5` → `v6` (Node 20 지원 종료 대응)

### 수정

- 읽기 타임아웃을 영구 오류로 처리해 한 번에 포기하던 문제 — 일시적 오류로 분류해 재시도 후 폴백
- `datetime.utcnow()` 폐기 경고 — timezone-aware 방식(`now_kst()`)으로 교체

---

## [0.2.0] - 2026-09-03

AWS 의존을 완전히 제거하고 GitHub만으로 운영하도록 전환했습니다.

### 변경

- **AWS 제거**
  - `boto3` / S3 읽기·쓰기 삭제, 외부 런타임 의존성 없음
  - 데이터는 저장소 내 파일로 보관하고 Actions가 실행 후 커밋
  - 워크플로에서 `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `S3_BUCKET_NAME` 제거
- **`history.json` → `briefings.json`** 으로 개명
  - 읽기는 `briefings.json` 우선, 없으면 기존 `history.json`으로 폴백
- Slack 대시보드 링크를 S3 웹사이트 주소 대신 `SITE_BASE_URL`(GitHub Pages) 기준으로 변경
  - 값이 없으면 링크 대신 안내 문구 출력

### 추가

- `index.html` — 모닝 팩터 대시보드 (차트 + 항목별 AI 분석)
  - 포트폴리오 키를 저장 데이터와 맞춰 `SPCX.O` → `SPCX` 정렬 (구 키 호환 유지)
- 기존 S3 `history.json` 데이터를 `briefings.json`으로 이관

---

## [0.1.0] - 2026-09-03

최초 구성. AWS Lambda + EventBridge에서 GitHub Actions로 스케줄러를 옮겼습니다.

### 추가

- 마켓 브리핑 수집·분석 스크립트 (`lambda_function.py`)
  - 시세: 환율/지수/원자재/보유 종목, 유가(오피넷), 공포탐욕지수, 뉴스(한경 RSS)
  - Gemini 분석 후 등락률·가격은 **코드가 직접 계산해 문장 앞에 삽입**
    (LLM이 숫자를 쓰지 못하게 해 수치 오류를 원천 차단)
  - `briefings.json` 누적 저장 + `raw` / `analysis` / `evidence` / `metadata` 계층 저장
  - 주간·월간 HTML 리포트 생성 및 Slack 발송
- CLI 진입점 — `--mode daily|weekly|monthly`, `--send-notification true|false`
- GitHub Actions 워크플로 4종

### 스케줄 (Asia/Seoul)

| 구분 | 시각 | 동작 |
|------|------|------|
| 아침 | 월~토 07:30 | 수집 + AI 분석 + Slack 브리핑 |
| 장마감 | 월~금 16:00 | 수집 + AI 분석 (알림 없음) |
| 주간 | 토 07:30 | 한 주 집계 리포트 |
| 월간 | 매달 1일 07:30 | 지난달 집계 리포트 |

---

## 데이터 스키마 버전

코드와 별개로 저장 데이터에도 버전을 기록합니다.

| 상수 | 현재 값 | 의미 |
|------|---------|------|
| `ANALYSIS_VERSION` | `0.1` | 심볼별 문단 텍스트 분석 방식 |
| `PROMPT_VERSION` | `market-analysis-0.1` | 프롬프트 구조 버전 |

`analysis/market/YYYY/MM/DD/{type}-v{ANALYSIS_VERSION}.json` 형태로 저장되며,
분석 방식이 바뀌면 버전을 올려 기존 파일을 덮어쓰지 않고 누적합니다.
