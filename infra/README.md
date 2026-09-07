# EventBridge 스케줄러

GitHub Actions 워크플로를 정시에 깨우는 외부 시계입니다.

## 왜 필요한가

GitHub의 `schedule` 이벤트는 best-effort라 보장이 없습니다. 실제 관측(2026-09-05 ~ 09-07):

- 실행이 예정 시각 대비 **8분 ~ 168분** 지연
- 아침 5개 슬롯 중 2개, 장마감 슬롯은 여러 날 통째로 미발생
- 09-07 장마감은 16:00 KST 슬롯이 16:40까지 발생하지 않음

반면 `workflow_dispatch`로 띄운 실행은 스케줄 큐를 타지 않고 **몇 초 안에 시작**합니다.
그래서 EventBridge가 cron으로 GitHub REST API를 호출해 워크플로를 깨웁니다.

```
EventBridge Rule (cron, UTC)
  └─> API Destination  POST api.github.com/repos/.../workflows/{file}/dispatches
        └─> Connection  Authorization: Bearer <PAT>
```

Lambda도 S3도 없습니다. AWS는 **시계 역할만** 하고 데이터는 전부 저장소에 남습니다.

## 시계가 둘인 이유

저장소의 `.github/workflows/*.yml`에 있는 cron은 **그대로 둡니다.**
EventBridge가 주 시계, GitHub cron이 백업입니다. AWS가 죽거나 PAT가 만료돼도
지금 수준으로 되돌아갈 뿐 브리핑이 멈추지는 않습니다.

시계가 둘이어도 중복 수집이나 중복 Slack 알림은 생기지 않습니다. dispatch가
idempotent하기 때문입니다.

| 워크플로 | 보내는 입력 | 중복 방지 |
|---|---|---|
| `daily.yml` | `session=auto` | 창 판정 + `metadata/.../{morning,close}.json` 완료 검사 |
| `weekly.yml` | 없음 (`force=false` 기본) | `--skip-if-done` → `reports/YYYY-Www.html` |
| `monthly.yml` | 없음 (`force=false` 기본) | `--skip-if-done` → `reports/YYYY-MM-monthly.html` |

`session=close`가 아니라 `auto`를 보내는 게 핵심입니다. `close`를 명시하면
중복 검사를 무시하고 강제 실행되므로, 자동 스케줄에서는 쓰지 않습니다.

## 스케줄

EventBridge cron은 UTC입니다. 한국은 서머타임이 없어 항상 +9시간 고정입니다.
UTC 22~23시대 슬롯은 KST로 다음 날이라 요일을 하루 앞당겨 적었습니다.

| 규칙 | cron (UTC) | KST |
|---|---|---|
| `morning` | `cron(30 22 ? * SUN-FRI *)` | 월~토 07:30 |
| `morning-retry` | `cron(10 23 ? * SUN-FRI *)` | 월~토 08:10 |
| `close` | `cron(0 7 ? * MON-FRI *)` | 월~금 16:00 |
| `close-retry` | `cron(40 7 ? * MON-FRI *)` | 월~금 16:40 |
| `weekly` | `cron(35 22 ? * FRI *)` | 토 07:35 |
| `weekly-retry` | `cron(15 23 ? * FRI *)` | 토 08:15 |
| `monthly` | `cron(40 22 L * ? *)` | 1일 07:40 |
| `monthly-retry` | `cron(20 23 L * ? *)` | 1일 08:20 |

`-retry` 슬롯은 첫 실행이 **실패했을 때**를 위한 것입니다. AI 호출이 실패하면
metadata가 기록되지 않으므로 보충 슬롯이 같은 세션을 다시 시도하고,
성공했으면 즉시 종료됩니다.

월간은 cron으로 "KST 1일"을 직접 쓸 수 없습니다. UTC 기준 전월 마지막 날 22:40이
KST 1일 07:40이므로 day-of-month에 `L`(마지막 날)을 씁니다. 덕분에 헛도는
실행 없이 매달 정확히 한 번만 깨웁니다.

## 배포

### 1. 토큰 발급

[fine-grained PAT](https://github.com/settings/personal-access-tokens/new)을 만듭니다.

- Repository access: `market-factor`만 선택
- Permissions → Repository permissions → **Actions: Read and write**

다른 권한은 필요 없습니다. 만료는 최대 1년이라 **갱신 일정을 잡아두세요.**
만료되면 dispatch가 401로 조용히 실패하고 GitHub cron만 남습니다.

### 2. 스택 배포

```bash
aws cloudformation deploy \
  --template-file infra/eventbridge-dispatch.yaml \
  --stack-name market-factor-scheduler \
  --region ap-northeast-2 \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides GitHubToken=github_pat_xxx
```

### 3. 동작 확인

규칙을 기다리지 않고 바로 확인하려면 같은 요청을 직접 보내봅니다.
성공하면 `204 No Content`가 오고 Actions 탭에 실행이 뜹니다.

```bash
curl -i -X POST \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer $GITHUB_PAT" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  https://api.github.com/repos/levi-shin/market-factor/actions/workflows/daily.yml/dispatches \
  -d '{"ref":"main","inputs":{"session":"auto"}}'
```

규칙이 실제로 호출됐는지는 CloudWatch에서 봅니다.

```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/Events --metric-name InvocationsFailedToBeSentToDlq \
  --dimensions Name=RuleName,Value=market-factor-scheduler-close \
  --start-time "$(date -u -d '1 day ago' +%FT%TZ)" \
  --end-time "$(date -u +%FT%TZ)" --period 3600 --statistics Sum
```

### 토큰 교체

Secrets Manager의 시크릿을 직접 고치면 안 됩니다. EventBridge가 관리하는
시크릿이라 Connection을 통해 바꿔야 합니다. 스택을 새 토큰으로 다시 배포하거나:

```bash
aws events update-connection \
  --name market-factor-scheduler-github \
  --authorization-type API_KEY \
  --auth-parameters '{"ApiKeyAuthParameters":{"ApiKeyName":"Authorization","ApiKeyValue":"Bearer github_pat_new"}}'
```

## 비용

월 200회 남짓 호출하므로 사실상 0원입니다.

| 항목 | 단가 | 월 |
|---|---|---|
| API Destination 호출 | $0.20 / 1M | 약 $0.00004 |
| Secrets Manager | API Destination 요금에 포함 | $0 |
| Lambda / S3 | 사용 안 함 | $0 |

Connection이 Secrets Manager에 시크릿을 만들지만, 보관·조회 비용은 API Destination
요금에 포함되어 별도 청구되지 않습니다.

## 구현 메모

- `User-Agent`는 EventBridge가 `Amazon/EventBridge/ApiDestinations`로 고정합니다.
  덮어쓸 수 없지만 GitHub의 User-Agent 필수 조건은 이걸로 충족됩니다.
- `Accept`, `X-GitHub-Api-Version`은 Connection의 `InvocationHttpParameters`에
  넣었습니다. EventBridge가 제거하는 헤더 목록에 없어서 그대로 전달됩니다.
- API Destination 하나를 워크플로 4개가 공유합니다. 엔드포인트 경로의 `*`를
  규칙마다 `PathParameterValues`로 채웁니다.
- EventBridge **Scheduler**(신규 서비스)는 API Destination을 타깃으로 쓸 수 없습니다.
  템플릿/universal 타깃만 지원해서 Lambda를 끼워야 하므로, 여기서는 예전 방식인
  EventBridge **규칙(rule)** 을 씁니다.
