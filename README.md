# market-factor

국내외 시장·환율·원자재·보유종목을 매일 자동 수집해 AI가 등락 원인을 분석하고, 아침/장마감 브리핑과 주간·월간 리포트를 Slack으로 보내는 개인용 자동화 시스템입니다.

## 구성

- `lambda_function.py` — AWS Lambda 핸들러 (시세 수집, Gemini 분석, S3 저장, Slack 알림)
- `infra/eventbridge-schedules.yaml` — EventBridge Scheduler 정의
- `requirements.txt` — Python 의존성

## EventBridge 스케줄 (Asia/Seoul)

| 이름 | 시각 | cron | Input |
|------|------|------|-------|
| market-briefing-morning | 월~토 07:30 KST | `cron(30 7 ? * MON-SAT *)` | `{"send_notification": true}` |
| market-briefing-close | 월~금 16:00 KST | `cron(0 16 ? * MON-FRI *)` | `{"send_notification": false}` |
| market-briefing-weekly | 토요일 07:30 KST | `cron(30 7 ? * SAT *)` | `{"mode": "weekly"}` |
| market-briefing-monthly | 매달 1일 07:30 KST | `cron(30 7 1 * ? *)` | `{"mode": "monthly"}` |

- **아침**: 시세 수집 + AI 분석 + Slack 브리핑 발송
- **장마감**: 시세/분석 갱신만 수행 (알림 미발송)
- **주간**: 한 주 집계 리포트 (토요일)
- **월간**: 한 달 집계 리포트 (매월 1일)
