# market-factor

국내외 시장·환율·원자재·보유종목을 매일 자동 수집해 AI가 등락 원인을 분석하고, 아침/장마감 브리핑과 주간·월간 리포트를 Slack으로 보내는 개인용 자동화 시스템입니다.

스케줄러는 **GitHub Actions** (`schedule` cron)로 돌립니다. (AWS Lambda / EventBridge 불필요)

## 구성

- `lambda_function.py` — 시세 수집, Gemini 분석, S3 저장, Slack 알림 (CLI 진입점 포함)
- `.github/workflows/` — 아침 / 장마감 / 주간 / 월간 스케줄
- `requirements.txt` — Python 의존성

## 스케줄 (Asia/Seoul)

| Workflow | 시각 | GitHub cron (UTC) | 실행 |
|----------|------|-------------------|------|
| `morning.yml` | 월~토 07:30 KST | `30 22 * * 0-5` | `--mode daily --send-notification true` |
| `close.yml` | 월~금 16:00 KST | `0 7 * * 1-5` | `--mode daily --send-notification false` |
| `weekly.yml` | 토요일 07:30 KST | `30 22 * * 5` | `--mode weekly` |
| `monthly.yml` | 매달 1일 07:30 KST | `30 22 28-31 * *` + KST 1일 가드 | `--mode monthly` |

- **아침**: 시세 수집 + AI 분석 + Slack 브리핑
- **장마감**: 시세/분석 갱신만 (알림 없음)
- **주간**: 한 주 집계 HTML 리포트 + Slack
- **월간**: 지난달 집계 HTML 리포트 + Slack

> GitHub `schedule`은 UTC만 지원하며, 부하에 따라 수 분~수십 분 지연될 수 있습니다.

## GitHub Secrets

Repository → Settings → Secrets and variables → Actions 에 아래를 등록하세요.

| Secret | 설명 |
|--------|------|
| `AWS_ACCESS_KEY_ID` | S3 읽기/쓰기 IAM 키 |
| `AWS_SECRET_ACCESS_KEY` | S3 읽기/쓰기 IAM 시크릿 |
| `AWS_REGION` | (선택) 기본 `ap-northeast-2` |
| `S3_BUCKET_NAME` | history/리포트 버킷명 |
| `GEMINI_API_KEY` | Google Gemini API 키 |
| `GEMINI_MODEL` | (선택) 우선 사용할 모델명 |
| `SLACK_WEBHOOK_URL` | Incoming Webhook URL |

## 로컬 / 수동 실행

```bash
pip install -r requirements.txt
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_DEFAULT_REGION=ap-northeast-2
export S3_BUCKET_NAME=...
export GEMINI_API_KEY=...
export SLACK_WEBHOOK_URL=...

python lambda_function.py --mode daily --send-notification true
python lambda_function.py --mode daily --send-notification false
python lambda_function.py --mode weekly
python lambda_function.py --mode monthly
```

Actions 탭에서 각 workflow의 **Run workflow**로도 수동 실행할 수 있습니다.
