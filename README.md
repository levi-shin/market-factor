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

| Workflow | 시각 | GitHub cron (UTC) | 실행 |
|----------|------|-------------------|------|
| `morning.yml` | 월~토 07:30 KST | `30 22 * * 0-5` | `--mode daily --send-notification true` |
| `close.yml` | 월~금 16:00 KST | `0 7 * * 1-5` | `--mode daily --send-notification false` |
| `weekly.yml` | 토요일 07:30 KST | `30 22 * * 5` | `--mode weekly` |
| `monthly.yml` | 매달 1일 07:30 KST | `30 22 28-31 * *` + KST 1일 가드 | `--mode monthly` |
| `reanalyze.yml` | 수동 | — | `--mode reanalyze` |

GitHub `schedule`은 UTC만 지원하고 실행이 보장되지 않아 건너뛸 수 있습니다.
그래서 아침·장마감에는 보충 슬롯을 두었습니다.

| 구분 | 정시 | 보충 |
|------|------|------|
| 아침 | 07:30 | 08:30, 09:30 |
| 장마감 | 16:00 | 17:00, 18:00 |

보충 슬롯은 `--skip-if-done`으로 동작해, 그날 이미 성공했으면 즉시 종료합니다.
따라서 Gemini 호출은 하루 1회만 나갑니다. 수동 실행은 이 검사를 무시하고 항상 실행됩니다.

## GitHub Secrets

| Secret | 설명 |
|--------|------|
| `GEMINI_API_KEY` | Google Gemini API 키 |
| `SLACK_WEBHOOK_URL` | Incoming Webhook URL |
| `SITE_BASE_URL` | (선택) 대시보드/리포트 링크 베이스. 예: `https://levi-shin.github.io/market-factor` |
| `GEMINI_MODEL` | (선택) 우선 사용할 모델명 |

## GitHub Pages (대시보드)

1. Settings → Pages → Source: **Deploy from a branch**
2. Branch: `main` / `/ (root)`
3. `SITE_BASE_URL`에 Pages URL을 넣으면 Slack 링크가 대시보드로 연결됩니다.

## 로컬 실행

```bash
export GEMINI_API_KEY=...
export SLACK_WEBHOOK_URL=...
export SITE_BASE_URL=https://levi-shin.github.io/market-factor   # 선택

python lambda_function.py --mode daily --send-notification true
python lambda_function.py --mode daily --send-notification false
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
