# market-factor

국내외 시장·환율·원자재·보유종목을 매일 자동 수집해 AI가 등락 원인을 분석하고, 아침/장마감 브리핑과 주간·월간 리포트를 Slack으로 보내는 개인용 자동화 시스템입니다.

**GitHub Actions만 사용합니다.** AWS Lambda / S3 / EventBridge는 쓰지 않습니다.

## 구성

- `lambda_function.py` — 시세 수집, Gemini 분석, 로컬 JSON/HTML 저장, Slack 알림
- `index.html` — 모닝 팩터 대시보드 (`briefings.json` 기반)
- `briefings.json` — 일별 브리핑 누적 데이터 (Actions가 갱신 후 커밋)
- `reports/` — 주간·월간 HTML 리포트
- `raw/` · `analysis/` · `evidence/` · `metadata/` — 일자별 상세 데이터
- `.github/workflows/` — 아침 / 장마감 / 주간 / 월간 / 재분석 스케줄
- `CHANGELOG.md` — 변경 이력

## 스케줄 (Asia/Seoul)

| Workflow | 대상 | 실행 |
|----------|------|------|
| `daily.yml` | 아침 + 장마감 | `--mode auto` (시각으로 세션 판별) |
| `weekly.yml` | 토요일 오전 | `--mode weekly --skip-if-done` |
| `monthly.yml` | 매달 1일 오전 | `--mode monthly --skip-if-done` |
| `reanalyze.yml` | 수동 | `--mode reanalyze` |

네 워크플로 모두 **정시 슬롯 하나에 의존하지 않고 창 안에서 여러 번 시도**하며,
이미 완료된 실행은 즉시 종료합니다.

| 대상 | 창 (KST) | 완료 판정 |
|------|----------|-----------|
| 아침 | 토요일 포함 월~토 07:30~12:00 | `metadata/.../morning.json` |
| 장마감 | 월~금 16:00~22:00 | `metadata/.../close.json` |
| 주간 | 토 07:30~11:00 | `reports/YYYY-Www.html` |
| 월간 | 1일 07:30~11:00 | `reports/YYYY-MM-monthly.html` |

주간·월간은 수동 실행 시 `force` 입력을 켜면 이미 만든 리포트도 다시 생성합니다.

### 일간 브리핑이 도는 방식

GitHub `schedule`은 실행이 보장되지 않습니다. 정시 슬롯 하나에 의존하면
그 슬롯이 누락될 때 그날 브리핑이 통째로 빠집니다.
(2026-09-04 관측: 아침이 5시간 지연, 장마감은 3개 슬롯 전부 미발생)

그래서 `daily.yml`은 **창(window) 안에서 매시간** 시도합니다.

| 세션 | 창 (KST) | 요일 | Slack |
|------|----------|------|-------|
| 아침 | 07:30 ~ 12:00 | 월~토 | 발송 |
| 장마감 | 16:00 ~ 22:00 | 월~금 | 미발송 |

`--mode auto`가 현재 KST 시각으로 세션을 고르고, **그날 이미 성공했으면 즉시 종료**합니다.
따라서 창 안의 슬롯이 몇 개 누락돼도 나머지 하나만 걸리면 그날 실행이 채워지고,
실제 수집·Gemini 호출은 세션당 1회만 일어납니다.

완료 판정 기준은 `metadata/market/YYYY/MM/DD/{morning,close}.json`의 `status == "published"`입니다.
AI가 실패한 날은 metadata가 기록되지 않으므로 남은 슬롯이 자동으로 재시도합니다.

수동 실행(`workflow_dispatch`)은 `session` 입력으로 `auto` / `morning` / `close`를 고를 수 있고,
`morning`·`close`를 직접 지정하면 중복 검사를 무시하고 강제 실행합니다.

## GitHub Secrets

| Secret | 설명 |
|--------|------|
| `GEMINI_API_KEY` | Google Gemini API 키 |
| `SLACK_WEBHOOK_URL` | Incoming Webhook URL |
| `SITE_BASE_URL` | (선택) 대시보드/리포트 링크 베이스. 비워두면 `CNAME` 값을 사용 |
| `GEMINI_MODEL` | (선택) 우선 사용할 모델명 |

## GitHub Pages (대시보드)

1. Settings → Pages → Source: **Deploy from a branch**
2. Branch: `main` / `/ (root)`

Slack 링크에 쓰이는 주소는 다음 순서로 결정됩니다.

1. `SITE_BASE_URL` Secret
2. 저장소의 `CNAME` (커스텀 도메인, 현재 `briefings.1125labs.com`)

커스텀 도메인을 쓰고 있다면 Secret은 설정하지 않아도 됩니다.

## 로컬 실행

```bash
export GEMINI_API_KEY=...
export SLACK_WEBHOOK_URL=...
export SITE_BASE_URL=https://levi-shin.github.io/market-factor   # 선택

# 현재 KST 시각으로 세션 판별 (Actions가 쓰는 방식)
python lambda_function.py --mode auto

# 세션 직접 지정
python lambda_function.py --mode daily --send-notification true    # 아침
python lambda_function.py --mode daily --send-notification false   # 장마감
python lambda_function.py --mode weekly
python lambda_function.py --mode monthly

# 시세 재수집 없이 그날 AI 분석만 다시 생성
python lambda_function.py --mode reanalyze
```

결과 파일(`briefings.json`, `reports/` 등)은 저장소에 쓰입니다. Actions에서는 실행 후 자동 커밋됩니다.

`DATA_ROOT`를 지정하면 다른 디렉터리에 쓸 수 있어 로컬 테스트에 유용합니다.

## 데이터 안정성

- 모든 파일은 `.tmp` 기록 후 `os.replace`로 교체합니다. 도중에 중단돼도 기존 파일이 깨지지 않습니다.
- AI 분석이 실패해도 수집한 시세는 저장·커밋되며, 워크플로는 실패로 표시되고 Slack 알림이 갑니다.
- 같은 날 이전 실행의 분석이 남아 있으면 빈 값으로 덮어쓰지 않고 보존합니다.
