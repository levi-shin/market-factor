import os
import json
import logging
import datetime
import urllib.request
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
import re
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

MY_PORTFOLIO_TICKERS = [
    ("NVDA", "엔비디아"),
    ("AAPL", "애플"),
    ("TSLA", "테슬라"),
    ("005930.KS", "삼성전자"),
    ("MSFT", "마이크로소프트"),
    # 2026-06-12 나스닥 상장 완료. 상장 전 임시 표기였던 "SPCX.O" 대신
    # 실제 상장 티커 "SPCX"를 사용해야 Yahoo Finance에서 정상 조회됨.
    ("SPCX", "스페이스X(SPCX)"),
    # Global X Robotics & Artificial Intelligence ETF (나스닥 상장, 티커: BOTZ)
    ("BOTZ", "로보틱스&AI ETF(BOTZ)"),
]

def clean_str(val):
    if not val:
        return ""
    return str(val).strip().strip("[]'\"` ")


# ==========================================
# 로컬 파일 저장 (GitHub 저장소 / Actions 커밋)
# ==========================================
# AWS S3 없이 저장소 안의 JSON/HTML로 관리. Actions가 실행 후 git commit/push.

def data_root():
    override = clean_str(os.environ.get("DATA_ROOT", ""))
    return Path(override) if override else Path(__file__).resolve().parent


def site_base_url():
    return clean_str(os.environ.get("SITE_BASE_URL", "")).rstrip("/")


def briefings_path():
    return data_root() / "briefings.json"


def legacy_briefings_path():
    return data_root() / "history.json"


def dashboard_url(path=""):
    base = site_base_url()
    if not base:
        return path or "index.html"
    return f"{base}/{path.lstrip('/')}" if path else base


def save_json_file(rel_path, payload):
    target = data_root() / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(target.relative_to(data_root()))


def save_text_file(rel_path, content, encoding="utf-8"):
    target = data_root() / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding=encoding)
    return str(target.relative_to(data_root()))


def http_get(url, headers=None, timeout=8):
    url = clean_str(url)
    req_headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode('utf-8', errors='ignore')

def http_post_json(url, payload, headers=None, timeout=30):
    # NOTE: url은 clean_str()으로 정제하지 않음.
    # clean_str은 문자열 양끝의 [ ] ' " ` 를 제거하는데, 정상적인 URL 끝에
    # 우연히 그런 문자가 오는 경우는 없어야 하지만, 혹시라도 마크다운 링크
    # 형태([...](...))가 실수로 섞여 들어오면 이 정제로는 못 잡아내고
    # 오히려 문제를 숨길 수 있어 여기서는 순수 문자열 그대로 사용.
    data = json.dumps(payload).encode('utf-8')
    req_headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    if headers:
        req_headers.update(headers)

    req = urllib.request.Request(url, data=data, headers=req_headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.read().decode('utf-8')
    except urllib.error.HTTPError as e:
        # 에러 응답 바디를 로그로 남겨야 원인(잘못된 URL, 모델명, API 키 등)을
        # 바로 파악할 수 있음. 기존 코드는 이 바디를 그냥 버렸었음.
        try:
            err_body = e.read().decode('utf-8', errors='ignore')
        except Exception:
            err_body = "(에러 바디 읽기 실패)"
        logger.error(f"HTTP {e.code} 에러 응답 (url={url}): {err_body}")
        raise

def get_weather():
    try:
        url = "https://api.open-meteo.com/v1/forecast?latitude=37.5665&longitude=126.9780&current=temperature_2m,relative_humidity_2m,apparent_temperature,precipitation&timezone=Asia%2FSeoul"
        res = json.loads(http_get(url, timeout=5))
        curr = res.get("current", {})
        temp = curr.get("temperature_2m")
        feel = curr.get("apparent_temperature")
        humidity = curr.get("relative_humidity_2m")
        precip = curr.get("precipitation", 0)
        status = "비 🌧️" if precip > 0 else "맑음/구름 🌤️"
        return f"{status} {temp}℃ (체감 {feel}℃, 습도 {humidity}%)", {"temp": temp, "humidity": humidity}
    except Exception as e:
        logger.error(f"Weather error: {e}")
        return "데이터 수집 지연", {}

def get_gasoline_prices():
    raw_data = {"gasoline": None, "premium_gasoline": None, "diesel": None}
    diff_data = {"gasoline": 0.0, "premium_gasoline": 0.0, "diesel": 0.0}
    try:
        url = "http://www.opinet.co.kr/api/avgAllPrice.do?out=xml&certkey=14nKCwjg75XwwfdWbvTqu5nssp4MRNr1y3ei1vQ2Rk"
        xml_data = http_get(url, timeout=6)
        root = ET.fromstring(xml_data)
        code_map = {"B027": "gasoline", "B034": "premium_gasoline", "D047": "diesel"}
        name_map = {"B027": "전국 평균 일반휘발유", "B034": "전국 평균 고급휘발유", "D047": "전국 평균 자동차용경유"}
        results = {}
        for oil in root.findall(".//OIL"):
            prodcd = oil.findtext("PRODCD", "").strip()
            if prodcd in code_map:
                price = float(oil.findtext("PRICE", "0").replace(",", ""))
                diff = float(oil.findtext("DIFF", "0").strip().replace("+", "").replace(",", ""))
                sign = "+" if diff > 0 else ""
                results[prodcd] = f"• {name_map[prodcd]}: {price:,.2f}원/L ({sign}{diff:,.2f}원)"
                raw_data[code_map[prodcd]] = price
                diff_data[code_map[prodcd]] = diff
        text = "\n".join([results[k] for k in ["B027", "B034", "D047"] if k in results])
        return text, raw_data, diff_data
    except Exception as e:
        logger.error(f"Opinet error: {e}")
        return "• 전국 평균 유가: 데이터 수집 지연", raw_data, diff_data

# get_market_data()와, 나중에 pct 재계산 후 텍스트를 다시 조립하는
# build_market_text()가 같은 순서/이름을 써야 하므로 모듈 상수로 분리.
MARKET_ITEMS = [
    ("KRW=X", "달러/원 환율", "usdkrw"),
    ("^TNX", "미국 10년물 국채금리", "us10y"),
    ("^GSPC", "S&P 500", "sp500"),
    ("^IXIC", "나스닥", "nasdaq"),
    ("^KS11", "코스피 (마감)", "kospi"),
    ("GC=F", "국제 금 (온스)", "gold_intl"),
    ("CL=F", "WTI 국제유가 (배럴)", "wti"),
    ("HG=F", "구리 (파운드)", "copper"),
]


def get_stock_price_any(symbol, name):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=2d"
        data = json.loads(http_get(url, timeout=4))
        meta = data['chart']['result'][0]['meta']
        curr = meta.get('regularMarketPrice')
        prev = meta.get('chartPreviousClose') or meta.get('previousClose')
        if curr is not None and prev is not None and prev != 0:
            pct = ((curr - prev) / prev) * 100
            unit = "%" if "금리" in name else ""
            is_usd = (".KS" not in symbol and "KRW" not in symbol and "코스피" not in name and "금리" not in name)
            currency_symbol = "$" if is_usd else ""
            return f"• {name}: {currency_symbol}{curr:,.2f}{unit} ({pct:+.2f}%)", curr, pct
        elif curr is not None:
            return f"• {name}: {curr:,.2f}", curr, 0.0
    except Exception:
        pass

    try:
        clean_sym = symbol.replace(".O", "").replace(".N", "")
        url = f"https://m.stock.naver.com/api/stock/{clean_sym}.O/integration"
        headers = {"Referer": "https://m.stock.naver.com/"}
        res = json.loads(http_get(url, headers=headers, timeout=4))
        deal_trend = res.get("dealTrendInfos", [{}])[0]
        close_price = deal_trend.get("closePrice") or res.get("totalInfos", [{}])[0].get("closePrice")
        rate = deal_trend.get("fluctuationsRatio") or res.get("totalInfos", [{}])[0].get("fluctuationsRatio")
        if close_price:
            curr = float(str(close_price).replace(",", ""))
            pct = float(str(rate).replace(",", "").replace("%", "")) if rate else 0.0
            return f"• {name}: ${curr:,.2f} ({pct:+.2f}%)", curr, pct
    except Exception:
        pass

    return f"• {name}: 데이터 수집 지연", None, 0.0

def get_portfolio_data():
    # 기존: 6개 티커를 하나씩 순차 호출 (최악의 경우 티커당 최대 8초 x 6 = 48초).
    # 병렬로 바꿔서 전체 소요 시간을 "가장 느린 티커 1개" 수준으로 줄임.
    lines_map = {}
    data_map = {}
    with ThreadPoolExecutor(max_workers=len(MY_PORTFOLIO_TICKERS)) as executor:
        future_to_sym = {
            executor.submit(get_stock_price_any, sym, name): (sym, name)
            for sym, name in MY_PORTFOLIO_TICKERS
        }
        for future in as_completed(future_to_sym):
            sym, name = future_to_sym[future]
            try:
                text, val, pct = future.result()
            except Exception as e:
                logger.error(f"포트폴리오 조회 실패 ({sym}): {e}")
                text, val, pct = f"• {name}: 데이터 수집 지연", None, 0.0
            lines_map[sym] = text
            data_map[sym] = {"name": name, "price": val, "change_rate": pct}

    # 원래 순서(MY_PORTFOLIO_TICKERS 순)를 유지해서 텍스트 조립
    lines = [lines_map[sym] for sym, _ in MY_PORTFOLIO_TICKERS]
    return "\n".join(lines), data_map

def get_market_data():
    items = MARKET_ITEMS
    fx_rate = 1369.6
    try:
        fx_data = json.loads(http_get("https://query1.finance.yahoo.com/v8/finance/chart/KRW=X?interval=1d&range=1d", timeout=4))
        fx_rate = fx_data['chart']['result'][0]['meta'].get('regularMarketPrice', 1369.6)
    except Exception:
        pass

    # 8개 티커를 병렬 조회 (순차 대비 최대 8배 가까이 단축).
    fetched = {}  # key -> (text, val, pct)
    with ThreadPoolExecutor(max_workers=len(items)) as executor:
        future_to_item = {
            executor.submit(get_stock_price_any, sym, name): (sym, name, key)
            for sym, name, key in items
        }
        for future in as_completed(future_to_item):
            sym, name, key = future_to_item[future]
            try:
                text, val, pct = future.result()
            except Exception as e:
                logger.error(f"시장 지표 조회 실패 ({key}): {e}")
                text, val, pct = f"• {name}: 데이터 수집 지연", None, 0.0
            fetched[key] = (text, val, pct)

    # 비트코인/국내 금은 위 티커 값(kospi 다음, 국제 금 다음)에 의존하므로
    # 병렬 조회 완료 후 순서대로 조립. 원래 코드의 출력 순서를 그대로 유지.
    results = []
    numeric_data = {}
    pct_data = {}
    for sym, name, key in items:
        text, val, pct = fetched[key]
        results.append(text)
        numeric_data[key] = val
        pct_data[key] = pct
        if key == "kospi":
            try:
                upbit_data = json.loads(http_get("https://api.upbit.com/v1/ticker?markets=KRW-BTC", timeout=4))
                btc_price = upbit_data[0]['trade_price']
                btc_change = upbit_data[0]['signed_change_rate'] * 100
                results.append(f"• 비트코인 (원화): {btc_price:,.2f} ({btc_change:+.2f}%)")
                numeric_data["btc"] = btc_price
                pct_data["btc"] = btc_change
            except Exception:
                pass
        elif key == "gold_intl":
            try:
                if val:
                    curr_g = (val * fx_rate) / 31.1035
                    pct_g = pct if pct is not None else 0.0
                    results.append(f"• 국내 금 (1g): {curr_g:,.2f} ({pct_g:+.2f}%)")
                    numeric_data["gold_kr"] = round(curr_g, 2)
                    pct_data["gold_kr"] = pct_g
            except Exception:
                pass
    return "\n".join(results), numeric_data, pct_data

def get_fear_and_greed():
    try:
        url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
        res = json.loads(http_get(url, timeout=5))
        score = round(res['fear_and_greed']['score'])
        rating = res['fear_and_greed']['rating'].lower()
        rating_kor = {"extreme fear": "극단적 공포 😱", "fear": "공포 😨", "neutral": "중립 😐", "greed": "탐욕 🤑", "extreme greed": "극단적 탐욕 🚀"}.get(rating, rating)
        return f"{score}점 ({rating_kor})", score
    except Exception:
        return "50점 (중립 😐)", 50

def get_news_headlines():
    # 기존: 제목 문자열만 반환.
    # 변경: evidence(근거) 저장을 위해 title 외에 url/발행시각도 같이 파싱해서
    # {"title":..., "url":..., "publishedAt":...} 형태의 딕셔너리 리스트로 반환.
    # 새로운 외부 API를 추가한 게 아니라, 이미 쓰던 한경 RSS 피드에서
    # 지금까지 버리고 있던 <link>/<pubDate> 태그를 추가로 읽는 것뿐임.
    try:
        xml_data = http_get("https://www.hankyung.com/feed/finance", timeout=6)
        root = ET.fromstring(xml_data)
        items = root.findall('.//item')[:6]
        headlines = []
        for item in items:
            title_el = item.find('title')
            if title_el is None or not title_el.text:
                continue
            link_el = item.find('link')
            pubdate_el = item.find('pubDate')
            headlines.append({
                "title": title_el.text.strip(),
                "url": clean_str(link_el.text) if link_el is not None and link_el.text else None,
                "publishedAt": pubdate_el.text.strip() if pubdate_el is not None and pubdate_el.text else None,
            })
        if headlines:
            return headlines
    except Exception as e:
        logger.error(f"뉴스 수집 오류: {e}")
    return [{"title": "글로벌 증시 및 금융 시장 주요 뉴스 수집 중", "url": None, "publishedAt": None}]

# ==========================================
# 3. Gemini 인과관계 심층 분석
# ==========================================

# ⚠️ 모델 세대교체 이력:
#   gemini-1.5-flash  -> 완전 셧다운 (404)
#   gemini-2.5-flash   -> 신규 사용자 대상 차단 (404, "no longer available to new users")
#   현재 GA(정식) 모델: gemini-3.6-flash, gemini-3.7-flash
#   generateContent API 자체는 아직 legacy로 지원되므로 엔드포인트는 그대로 두고
#   모델명만 최신으로 교체. 구글이 몇 달 간격으로 모델을 계속 갈아치우고 있어서,
#   1순위 모델이 또 막혀도 자동으로 다음 후보로 넘어가도록 폴백 리스트를 둠.
#
# 주의: os.environ.get(key, default)는 환경변수가 "아예 없을 때"만 default를
# 씀. 환경변수 GEMINI_MODEL을 빈 값("")으로 만들어두면 default가 무시되고
# 빈 문자열이 그대로 쓰여서 "models/:generateContent" 같은 깨진 URL이 됨.
# 그래서 clean_str() 결과가 빈 문자열이면 명시적으로 버리도록 처리.
_env_model = clean_str(os.environ.get("GEMINI_MODEL", ""))
# Google AI Studio(API 키) 기준 후보. 수요 폭주(503) 시 같은 모델에 붙잡지 않고
# 다음 후보로 빨리 넘기기 위해 다양하게 둠.
# 2026-09 기준 generateContent에서 살아있는 모델만 둔다.
# gemini-2.5-flash / 2.0-flash는 404(no longer available)라 제거함.
_DEFAULT_GEMINI_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
]
GEMINI_MODEL_FALLBACKS = []
for m in ([_env_model] if _env_model else []) + _DEFAULT_GEMINI_MODELS:
    if m and m not in GEMINI_MODEL_FALLBACKS:
        GEMINI_MODEL_FALLBACKS.append(m)
if not GEMINI_MODEL_FALLBACKS:
    GEMINI_MODEL_FALLBACKS = list(_DEFAULT_GEMINI_MODELS)

# ⚠️ 핵심 버그 수정: Gemini가 "실제 데이터를 봐라"는 지시문만으로는 숫자를
# 새로 지어내는 경우가 있었음 (예: 513.53 -> 507.29인데 "+0.44% 상승"이라고
# 답함 - 예시 템플릿 복사가 아니라 아예 새로운 오답을 만든 케이스).
# 그래서 Gemini에겐 "원인/영향" 서술만 맡기고, 등락 방향과 %/가격은 우리
# 코드가 직접 계산해서 각 항목 문장 맨 앞에 붙인다. 이러면 숫자가 틀릴
# 방법 자체가 없어짐 (LLM이 숫자를 아예 안 씀).

def _direction_word(pct):
    if pct is None:
        return "보합"
    return "상승" if pct >= 0 else "하락"


def _pct_str(pct):
    return f"{pct:+.2f}%" if pct is not None else "변동 없음"


def _fmt_num(value, decimals=1):
    return f"{value:,.{decimals}f}" if value is not None else "N/A"


def build_prefixed_reasons(reasons_dict, numeric_data, pct_data, portfolio_map, oil_data, oil_diff):
    # reasons_dict: Gemini가 생성한 {symbol: "원인+영향 서술문"} 딕셔너리
    # 반환: 각 문장 앞에 "OOO는 전 거래일 대비 X% 상승/하락한 Y를 기록했습니다."
    #        형태의, 우리 코드가 직접 계산한 정확한 문장이 붙은 딕셔너리
    if not reasons_dict:
        return reasons_dict

    result = dict(reasons_dict)

    # (필드key, 주어, numeric_data/pct_data 키, 통화기호, 단위, 소수자리)
    macro_specs = [
        ("usdkrw", "달러/원 환율은", "usdkrw", "", "원", 1),
        ("kospi", "코스피 지수는", "kospi", "", "포인트", 1),
        ("nasdaq", "나스닥 지수는", "nasdaq", "", "포인트", 1),
        ("sp500", "S&P 500 지수는", "sp500", "", "포인트", 1),
        ("wti", "WTI유가는", "wti", "$", "", 2),
        ("gold_intl", "국제 금 가격은", "gold_intl", "$", "", 1),
        ("btc", "비트코인은", "btc", "", "원", 0),
    ]
    for field_key, subject, data_key, currency, unit, decimals in macro_specs:
        if field_key not in result:
            continue
        value = numeric_data.get(data_key)
        pct = pct_data.get(data_key)
        if value is None:
            continue
        prefix = (f"{subject} 전 거래일 대비 {_pct_str(pct)} {_direction_word(pct)}한 "
                  f"{currency}{_fmt_num(value, decimals)}{unit}(으)로 마감했습니다. ")
        result[field_key] = prefix + (result[field_key] or "")

    # 보유 종목 (환율/포인트 표기가 종목마다 다름 - 국내(005930.KS)는 원화, 나머지는 달러)
    stock_specs = [
        ("NVDA", "엔비디아는"), ("AAPL", "애플은"), ("TSLA", "테슬라는"),
        ("MSFT", "마이크로소프트는"), ("SPCX", "스페이스X(SPCX)는"),
        ("BOTZ", "로보틱스&AI ETF(BOTZ)는"),
        ("005930.KS", "삼성전자는"),
    ]
    for sym, subject in stock_specs:
        if sym not in result:
            continue
        info = portfolio_map.get(sym)
        if not info or info.get("price") is None:
            continue
        price = info["price"]
        pct = info.get("change_rate")
        is_domestic = sym.endswith(".KS")
        currency = "" if is_domestic else "$"
        unit = "원" if is_domestic else ""
        prefix = (f"{subject} 전 거래일 대비 {_pct_str(pct)} {_direction_word(pct)}한 "
                  f"{currency}{_fmt_num(price, 2)}{unit}에 마감했습니다. ")
        result[sym] = prefix + (result[sym] or "")

    # 국내 유가는 %가 아니라 원 단위 등락폭(diff)으로 표기하는 게 관례
    oil_specs = [
        ("gasoline", "전국 평균 일반휘발유는"),
        ("premium_gasoline", "전국 평균 고급휘발유는"),
    ]
    for field_key, subject in oil_specs:
        if field_key not in result:
            continue
        price = oil_data.get(field_key)
        diff = oil_diff.get(field_key, 0.0)
        if price is None:
            continue
        word = "상승" if diff and diff > 0 else ("하락" if diff and diff < 0 else "보합")
        prefix = f"{subject} 전일 대비 {diff:+.2f}원 {word}한 {_fmt_num(price, 1)}원/L을 기록했습니다. "
        result[field_key] = prefix + (result[field_key] or "")

    return result


# ⚠️ 2026-09-03 실제 발생 사례: Gemini가 503("일시적으로 수요 폭주, 나중에
# 다시 시도하세요")을 반환했는데, 예전 코드는 404가 아니면 바로 포기해버려서
# 그날 하루 분석이 통째로 날아감. 503/502/500/429처럼 "일시적" 성격이 강한
# 응답은 같은 모델로 짧게 재시도하고, 그래도 안 되면 다음 후보 모델로 넘어감.
TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}
# 503/429(수요 폭주·쿼터)는 같은 모델 재시도 없이 즉시 다음 후보로 넘김
CAPACITY_HTTP_CODES = {429, 503}
GEMINI_RETRY_DELAY_SEC = 4
GEMINI_MAX_RETRIES_PER_MODEL = 2  # 일반 일시 오류(500/502/504): 같은 모델 최대 2번
GEMINI_HTTP_TIMEOUT = 90  # 장문 한국어 JSON — Actions에서도 여유 있게


def _is_transient_network_error(exc):
    # urllib timeout / 연결 끊김은 HTTP status가 아니라 URLError/TimeoutError로 옴.
    # 예전엔 Exception으로 바로 포기해서 타임아웃 1번에 분석이 통째로 날아감.
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", None)
        if isinstance(reason, (TimeoutError, OSError)):
            return True
        msg = str(reason or exc).lower()
        if "timed out" in msg or "timeout" in msg or "temporarily unavailable" in msg:
            return True
    msg = str(exc).lower()
    return "timed out" in msg or "timeout" in msg


def _payload_for_model(payload, model):
    # gemini-3.x만 thinkingConfig 사용. 구세대 모델엔 빼서 400을 피함.
    body = json.loads(json.dumps(payload))  # deep copy (dict-only)
    if not str(model).startswith("gemini-3"):
        gen = body.get("generationConfig") or {}
        gen.pop("thinkingConfig", None)
        if gen:
            body["generationConfig"] = gen
        elif "generationConfig" in body:
            del body["generationConfig"]
    return body


def _extract_gemini_text(res):
    # gemini-3 thinking 응답은 parts가 여러 개일 수 있음. text만 모은다.
    try:
        parts = res["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError):
        return ""
    chunks = []
    for part in parts:
        if isinstance(part, dict) and part.get("text"):
            chunks.append(part["text"])
    return "\n".join(chunks).strip()


def _parse_json_object(text_out):
    if not text_out:
        return None
    # 가장 바깥 { ... } 블록을 찾되, 파싱 성공할 때까지 후보를 줄여가며 시도
    start = text_out.find("{")
    end = text_out.rfind("}")
    if start < 0 or end <= start:
        return None
    candidate = text_out[start:end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        # greedy 매칭이 깨진 경우: 정규식으로 한 번 더
        m = re.search(r"\{.*\}", text_out, re.DOTALL)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None


def _is_usable_analysis(parsed, context_label="Gemini"):
    # "JSON만 파싱되면 성공"이 아니라, 실제로 쓸 문장이 있는지만 본다.
    if not isinstance(parsed, dict) or not parsed:
        logger.warning(f"{context_label} 응답 JSON이 비어 있거나 dict가 아님: {type(parsed)}")
        return False
    overall = clean_str(parsed.get("overall", "")) if isinstance(parsed.get("overall"), str) else ""
    nonempty = 0
    for k, v in parsed.items():
        if isinstance(v, str) and len(v.strip()) >= 40:
            nonempty += 1
        elif isinstance(v, dict):
            # weekly next_period_events 등
            if any(isinstance(x, str) and len(x.strip()) >= 10 for x in v.values()):
                nonempty += 1
    if context_label.startswith("주") or context_label.startswith("달") or "간 AI" in context_label:
        # 주간/월간: issue_analysis 또는 next_period_events
        issue = clean_str(parsed.get("issue_analysis", "")) if isinstance(parsed.get("issue_analysis"), str) else ""
        events = parsed.get("next_period_events") or parsed.get("next_week_events") or {}
        ok = bool(issue) or (isinstance(events, dict) and len(events) > 0)
        if not ok:
            logger.warning(f"{context_label} 주간/월간 필수 필드 부족: keys={list(parsed.keys())}")
        return ok
    if not overall or len(overall) < 40:
        logger.warning(f"{context_label} overall 부족(len={len(overall)}). keys={list(parsed.keys())}")
        return False
    if nonempty < 5:
        logger.warning(f"{context_label} 유효 필드 부족({nonempty}개). keys={list(parsed.keys())}")
        return False
    return True


def call_gemini_json(payload, headers, timeout=None, context_label="Gemini"):
    # 모델 폴백 리스트를 순서대로 시도하되, 각 모델마다 "일시적 오류/타임아웃"이면
    # 짧게 재시도하고, 그래도 실패하거나 영구적 오류(404 등)면 다음 모델로.
    timeout = timeout or GEMINI_HTTP_TIMEOUT
    logger.info(f"{context_label} 모델 후보(중복 제거): {GEMINI_MODEL_FALLBACKS}")
    for model in GEMINI_MODEL_FALLBACKS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        model_payload = _payload_for_model(payload, model)

        for attempt in range(1, GEMINI_MAX_RETRIES_PER_MODEL + 1):
            try:
                raw_res = http_post_json(url, model_payload, headers=headers, timeout=timeout)
                res = json.loads(raw_res)
                text_out = _extract_gemini_text(res)
                parsed = _parse_json_object(text_out)
                if parsed is None:
                    logger.error(f"{context_label} 응답에서 JSON을 찾지 못함 (모델: {model}): {text_out[:500]}")
                    break  # 이 모델 포기, 다음 모델
                if not _is_usable_analysis(parsed, context_label):
                    logger.error(f"{context_label} JSON은 왔지만 내용이 비어 있음 (모델: {model}, 시도: {attempt}). "
                                 f"미리보기: {str(parsed)[:300]}")
                    break  # 빈 성공 금지 — 다음 모델 시도
                nonempty = sum(1 for v in parsed.values() if isinstance(v, str) and len(v.strip()) >= 40)
                logger.info(f"✅ {context_label} 성공 (모델: {model}, 시도: {attempt}, "
                            f"필드 {len(parsed)}개/유효문장 {nonempty}개, overall {len(str(parsed.get('overall','')))}자)")
                return parsed, model

            except urllib.error.HTTPError as e:
                if e.code == 404:
                    logger.warning(f"모델 {model} 사용 불가(404). 다음 후보 모델로 넘어갑니다.")
                    break  # 이 모델은 재시도 의미 없음 -> 다음 모델로

                # 503/429: 수요 폭주 — 재시도해도 같은 모델이 또 막히는 경우가 많음.
                # 바로 다음 후보(3.7 / 2.5 / 2.0 등)로 넘어가서 성공 확률을 높임.
                if e.code in CAPACITY_HTTP_CODES:
                    logger.warning(f"{context_label} 수요 폭주(HTTP {e.code}, 모델: {model}). "
                                    f"같은 모델 재시도 없이 다음 후보로 넘어갑니다.")
                    break

                if e.code in TRANSIENT_HTTP_CODES and attempt < GEMINI_MAX_RETRIES_PER_MODEL:
                    logger.warning(f"{context_label} 일시적 오류(HTTP {e.code}, 모델: {model}, {attempt}번째 시도). "
                                    f"{GEMINI_RETRY_DELAY_SEC}초 후 같은 모델로 재시도합니다.")
                    time.sleep(GEMINI_RETRY_DELAY_SEC)
                    continue  # 같은 모델 재시도

                if e.code in TRANSIENT_HTTP_CODES:
                    logger.warning(f"{context_label} 일시적 오류(HTTP {e.code})가 재시도 후에도 계속됨 "
                                    f"(모델: {model}). 다음 후보 모델로 넘어갑니다.")
                    break  # 재시도 소진 -> 다음 모델로

                logger.error(f"{context_label} 호출 실패 (모델: {model}, HTTP {e.code}): {e}")
                return None, None  # 영구적 오류(인증 실패 등)로 판단, 바로 포기

            except Exception as e:
                if _is_transient_network_error(e) and attempt < GEMINI_MAX_RETRIES_PER_MODEL:
                    logger.warning(f"{context_label} 타임아웃/네트워크 오류(모델: {model}, {attempt}번째 시도): {e}. "
                                    f"{GEMINI_RETRY_DELAY_SEC}초 후 같은 모델로 재시도합니다.")
                    time.sleep(GEMINI_RETRY_DELAY_SEC)
                    continue
                if _is_transient_network_error(e):
                    logger.warning(f"{context_label} 타임아웃/네트워크 오류가 재시도 후에도 계속됨 "
                                    f"(모델: {model}): {e}. 다음 후보 모델로 넘어갑니다.")
                    break
                logger.error(f"{context_label} 호출 실패 (모델: {model}): {e}")
                return None, None

    logger.error(f"{context_label}: 모든 후보 모델 실패: {GEMINI_MODEL_FALLBACKS}")
    return None, None


def get_itemized_ai_analysis(market_data_text, portfolio_text, oil_prices_text, news_list):
    # 반환값: (reasons_dict, model_used) 튜플. 실패 시 (None, None).
    # model_used는 metadata에 "실제로 어떤 모델이 이 분석을 생성했는지" 남기기 위함.
    api_key = clean_str(os.environ.get("GEMINI_API_KEY", ""))
    if not api_key:
        logger.error("❌ ERROR: GEMINI_API_KEY 환경 변수가 없습니다.")
        return None, None

    # news_list는 이제 [{"title":..., "url":..., "publishedAt":...}, ...] 형태.
    # Gemini 프롬프트엔 지금까지처럼 제목만 넣음 (evidence 저장용 url/시각은
    # save_evidence_market()에서 별도로 사용).
    news_text = "\n".join([f"• {n['title']}" for n in news_list])
    logger.info(f"Gemini 모델 후보 순서: {GEMINI_MODEL_FALLBACKS}")

    prompt = f"""
당신은 대한민국 최고 수준의 월가 매크로 헤지펀드 및 여의도 수석 스트래티지스트입니다.
아래 각 항목마다 [왜 상승/하락했는지 구체적 원인]과 [이로 인해 시장/투자자에게 미치는 파급 영향]을 명확한 인과관계로 3~4문장씩 서술하세요.

[매우 중요 - 숫자/방향 서술 금지]
- 절대로 구체적인 등락 %, 가격, "상승"/"하락"이라는 단어를 본문에 직접 쓰지 마세요.
  (예: "3.76% 상승한 $217.55" 같은 표현 금지) 이유: 수치는 시스템이 별도로
  정확하게 계산해서 자동으로 앞에 붙이며, 당신이 숫자를 다시 쓰면 원본 데이터와
  불일치할 위험이 있어 절대 금지합니다.
- 대신 "이런 움직임의 원인"과 "그로 인한 파급 영향"만 서술하세요. 방향을 굳이
  언급해야 한다면 "이러한 흐름은", "이 같은 움직임은"처럼 숫자·단정적 방향
  단어 없이 에둘러 표현하세요.
- 데이터에 없는 사실을 추측하거나 지어내지 말고, 아래 [원시 데이터]와 뉴스
  헤드라인에 근거해서만 서술하세요.
- 스페이스X(SPCX)는 2026년 6월 12일 나스닥에 상장(IPO)을 완료한 상장 기업입니다.
  "비상장 기업이라 데이터가 없다" 등 사실과 다른 발언을 절대 하지 마세요.

반드시 마크다운(```json) 없이 순수 JSON 포맷으로만 출력하세요.

JSON 출력 포맷 (각 필드는 "원인 + 파급 영향"만, 숫자/방향 단어 없이):
{{
  "overall": "시장 종합 인과관계 총평 - 환율/금리/기술주/유가 간 연결고리와 투자 시사점을 3줄로 (숫자 나열보다 관계/맥락 위주로)",
  "usdkrw": "달러/원 환율 분석: 이런 흐름의 배경과 수출기업 실적 및 외인 수급에 미치는 영향",
  "kospi": "코스피 분석: 지수 움직임의 원인과 국내 증시 파급 영향",
  "nasdaq": "나스닥 분석: 움직임의 원인과 미국 성장주 밸류에이션 파급 효과",
  "sp500": "S&P 500 분석: 움직임의 원인과 미국 증시 전반의 리스크 심리 파급 효과",
  "wti": "국제유가(WTI) 분석: 원인과 정유/석유화학 및 수입물가 압력 영향",
  "gasoline": "일반휘발유 분석: 주유소 판매가 동향 및 국제유가 변동의 시차 반영",
  "premium_gasoline": "고급휘발유 분석: 가격 변동 배경 및 정제마진 영향",
  "NVDA": "엔비디아 분석: 원인(AI 가속기 칩 수요, 빅테크 CAPEX 등)과 AI 하드웨어 생태계 파급 영향",
  "AAPL": "애플 분석: 원인(Apple Intelligence, 서비스 매출 등)과 공급망 생태계 영향",
  "TSLA": "테슬라 분석: 원인(FSD/로보택시 기대감, 판매량 등)과 2차전지/자율주행 테마 영향",
  "005930.KS": "삼성전자 분석: 원인(외국인 수급, HBM 공급망 이슈 등)과 국내 반도체 섹터 영향",
  "MSFT": "마이크로소프트 분석: 원인(Azure 클라우드 AI 매출 등)과 기업용 소프트웨어 시장 영향",
  "SPCX": "스페이스X(SPCX) 분석: 원인(스타링크 가입자, 발사 일정, 락업 해제 등)과 민간 우주산업 투자 심리 파급 영향",
  "BOTZ": "로보틱스&AI ETF(BOTZ) 분석: 원인(로봇/자동화·AI 관련 편입 종목 실적 및 테마 자금 흐름 등)과 로보틱스/자동화 테마 투자 심리 파급 영향",
  "gold_intl": "국제/국내 금 분석: 원인(실질금리, 달러인덱스, 안전자산 선호 등)과 헷지 자산 영향",
  "btc": "비트코인 분석: 원인(현물 ETF 자금 흐름, 글로벌 유동성 등)과 가상자산 시장 전반 영향"
}}

[원시 데이터] (이 수치는 여기서만 참고하고, 본문에 그대로 다시 쓰지 마세요)
거시 지표:
{market_data_text}
보유 포트폴리오:
{portfolio_text}
국내 유가:
{oil_prices_text}
증시 및 금융 헤드라인:
{news_text}
"""
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        # gemini-3.x 계열은 기본적으로 내부 추론(thinking)을 거치는데, 이 작업은
        # 단순 요약/서술이라 굳이 깊은 추론이 필요 없음. thinking을 낮춰서
        # 응답 속도를 확보 (안 그러면 타임아웃 위험이 커짐).
        "generationConfig": {
            "thinkingConfig": {"thinkingLevel": "low"}
        }
    }
    headers = {"x-goog-api-key": api_key}

    # 14개 항목 x 3~4문장의 긴 한국어 출력 — 타임아웃/재시도는 GEMINI_HTTP_TIMEOUT·call_gemini_json에서 처리
    return call_gemini_json(payload, headers, GEMINI_HTTP_TIMEOUT, context_label="Gemini")

# ==========================================
# 3.5. 데이터 구조 뼈대 (raw/analysis/evidence/metadata)
# ==========================================
# ⚠️ 기존 briefings.json 저장(save_to_s3)은 대시보드(index.html)가 계속 참조함.
# (구 history.json은 읽기 폴백만 유지)
# 아래는 "이중 쓰기"로 추가되는 신규 계층(raw/analysis/...)이며, 실패해도
# 기존 흐름(Slack 알림 등)에 영향을 주지 않도록 lambda_handler에서 별도
# try/except로 감싸서 호출함. AWS S3 없이 저장소 로컬 경로에 기록.
#
# 설계 원칙(로드맵 문서 1번 참고):
#   - raw / analysis 는 절대 섞지 않는다
#   - 모든 레코드 본문에 date/domain/analysisType/version을 명시적으로 포함
#     (파일 경로만으로 식별하지 않음 - 나중에 DB 이관/API 응답으로 그대로
#     써도 자기 자신을 설명할 수 있어야 함)
#   - 분석은 append-only (버전을 올려가며 쌓임, 덮어쓰지 않음)
#   - domain 필드를 항상 넣어서 나중에 market 외 도메인이 추가돼도
#     경로 구조 자체는 안 바뀌게 함
#   - userId는 아직 없지만, 나중에 필드로 추가될 걸 배제하지 않는 형태로 둠
#     (지금은 넣지 않되, 구조상 끼워 넣기 쉬운 평평한 딕셔너리 형태 유지)

DOMAIN = "market"
ANALYSIS_VERSION = "0.1"   # 지금 쓰는 "심볼별 문단 텍스트" 분석 방식 = v0.1
PROMPT_VERSION = "market-analysis-0.1"


def now_kst():
    # timezone-aware UTC 기준 (datetime.utcnow 폐기 경고 회피)
    return datetime.datetime.now(datetime.timezone.utc).astimezone(
        datetime.timezone(datetime.timedelta(hours=9))
    )


def kst_date_str():
    kst_now = now_kst()
    return kst_now.strftime("%Y-%m-%d"), kst_now


def build_data_key(layer, domain, date_str, filename):
    # 예: raw/market/2026/09/01/morning.json
    y, m, d = date_str.split("-")
    return f"{layer}/{domain}/{y}/{m}/{d}/{filename}"


def build_s3_key(layer, domain, date_str, filename):
    # 하위 호환 별칭
    return build_data_key(layer, domain, date_str, filename)


def build_record_id(layer, domain, date_str, type_or_symbol, version=None):
    # 예: analysis-market-2026-09-01-morning-v0.1
    base = f"{layer}-{domain}-{date_str}-{type_or_symbol}"
    return f"{base}-v{version}" if version else base


def save_json_to_s3(key, payload):
    # 하위 호환 별칭 — 실제로는 로컬 파일에 저장
    return save_json_file(key, payload)


def save_json_to_repo(key, payload):
    path = save_json_file(key, payload)
    logger.info(f"JSON 저장: {path}")
    return path


def save_raw_market(date_str, analysis_type, numeric_data, pct_data, portfolio_map, oil_data, fear_score):
    # 원본 수치만 담음. AI 분석 결과는 절대 포함하지 않음.
    payload = {
        "date": date_str,
        "domain": DOMAIN,
        "analysisType": analysis_type,  # "morning" | "close"
        # 공용 시장 데이터와 개인 포트폴리오 데이터를 저장 구조에서도 계속
        # 분리 (로드맵 4번 원칙 - 나중에 사용자가 늘어나도 자연 확장 가능).
        "market": {
            "numeric": numeric_data,
            "pct": pct_data,
            "oil": oil_data,
            "fearScore": fear_score,
        },
        "portfolio": portfolio_map,
    }
    key = build_data_key("raw", DOMAIN, date_str, f"{analysis_type}.json")
    save_json_to_s3(key, payload)
    logger.info(f"raw 저장 완료: {key}")


def save_evidence_market(date_str, news_list):
    # 지금은 심볼별로 나눠서 검색하는 게 아니라(로드맵 B단계 이후 과제),
    # 그날 수집된 공통 뉴스 소스 전체를 하나의 evidence 파일로 저장.
    # title/url/publishedAt을 원문 그대로 보존해서, 나중에 원본 기사가
    # 사라지거나 바뀌어도 우리 쪽 기록은 그대로 남게 함.
    sources = []
    for idx, item in enumerate(news_list):
        sources.append({
            "id": f"evidence-{idx+1:03d}",
            "title": item.get("title"),
            "url": item.get("url"),
            "publishedAt": item.get("publishedAt"),
            "source": "한국경제",
        })
    payload = {
        "date": date_str,
        "domain": DOMAIN,
        "sources": sources,
    }
    key = build_data_key("evidence", DOMAIN, date_str, "news.json")
    save_json_to_s3(key, payload)
    logger.info(f"evidence 저장 완료: {key} (기사 {len(sources)}건)")


def save_analysis_market(date_str, analysis_type, reasons_dict):
    # append-only: 같은 날짜/타입이라도 버전을 올려서 저장하고 덮어쓰지 않음.
    # 지금은 항상 ANALYSIS_VERSION("0.1")만 생성하므로 실질적으로는 하루 1개씩
    # 쌓이지만, 나중에 mode=reanalyze가 생기면 여기에 v0.2, v1.0 등이
    # 추가로 쌓이는 구조. 기존 파일이 이미 있어도 걱정 없이 덮어써도 되는
    # "같은 버전 재실행" 케이스만 지금은 발생함 (같은 날 같은 analysis_type을
    # 하루 한 번만 돌리므로).
    payload = {
        "date": date_str,
        "domain": DOMAIN,
        "analysisType": analysis_type,
        "version": ANALYSIS_VERSION,
        "promptVersion": PROMPT_VERSION,
        "reasons": reasons_dict or {},
    }
    filename = f"{analysis_type}-v{ANALYSIS_VERSION}.json"
    key = build_data_key("analysis", DOMAIN, date_str, filename)
    save_json_to_s3(key, payload)
    logger.info(f"analysis 저장 완료: {key}")


def save_metadata_market(date_str, analysis_type, generated_at_iso, model_used, status):
    payload = {
        "date": date_str,
        "domain": DOMAIN,
        "analysisType": analysis_type,
        "generatedAt": generated_at_iso,
        "analysisVersion": ANALYSIS_VERSION,
        "promptVersion": PROMPT_VERSION,
        "model": model_used,
        "status": status,  # "published" | "failed"
    }
    key = build_data_key("metadata", DOMAIN, date_str, f"{analysis_type}.json")
    save_json_to_s3(key, payload)
    logger.info(f"metadata 저장 완료: {key}")


def save_new_data_structure(numeric_data, pct_data, portfolio_map, oil_data, fear_score,
                             news_list, reasons_dict, analysis_type, model_used):
    # 위 4개 저장 함수를 순서대로 호출하는 진입점. 이 함수 전체가
    # lambda_handler에서 별도 try/except로 감싸져서, 여기서 뭔가 실패해도
    # 기존 briefings.json 저장/Slack 알림에는 영향이 없음.
    date_str, kst_now = kst_date_str()
    save_raw_market(date_str, analysis_type, numeric_data, pct_data, portfolio_map, oil_data, fear_score)
    save_evidence_market(date_str, news_list)
    status = "published" if reasons_dict else "failed"
    save_analysis_market(date_str, analysis_type, reasons_dict)
    save_metadata_market(date_str, analysis_type, kst_now.isoformat(), model_used, status)


# ==========================================
# 3.7. pct(등락률) 재계산 - "직전 실행 대비"로 통일
# ==========================================
# ⚠️ 버그 배경: Yahoo Finance의 previousClose는 환율(KRW=X)·금(GC=F)·
# WTI(CL=F)·구리(HG=F)처럼 24시간 가까이 거래되는 자산에 대해, 우리가
# 매일 KST 기준으로 스냅샷 찍는 시점과 다른 기준점(예: 뉴욕 세션 마감)을
# 쓸 수 있음. 그 결과 Yahoo가 주는 pct와 "우리가 어제 실제로 기록했던
# 값 대비 오늘 값"이 서로 다른(심지어 부호가 반대인) 경우가 발생함.
#
# 해결: Yahoo의 pct를 그대로 믿지 않고, briefings.json의 "가장 최근
# 기록(=직전 실행 결과, 07:30 아니면 16:00)"과 직접 비교해서 우리가
# 등락률을 다시 계산한다. 하루 2번 도는 스케줄과 맞물려서, 이러면 항상
# "지난 브리핑 이후 얼마나 움직였는지"라는 명확한 기준이 생김.


def load_briefings(bucket_name=None):
    """briefings.json을 읽고, 없으면 구 history.json을 폴백으로 읽음."""
    for path in (briefings_path(), legacy_briefings_path()):
        try:
            if not path.exists():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            if path == legacy_briefings_path():
                logger.info(f"구 파일 {path.name}에서 브리핑 데이터를 읽었습니다 (마이그레이션 대상)")
            return data if isinstance(data, list) else []
        except Exception as e:
            if path == briefings_path():
                logger.warning(f"{path.name} 조회 실패, 폴백 시도: {e}")
                continue
            raise
    return []


def save_briefings(bucket_name=None, briefings=None):
    # 호출 호환: save_briefings(briefings) 또는 save_briefings(None, briefings)
    if briefings is None and bucket_name is not None and not isinstance(bucket_name, str):
        briefings = bucket_name
    path = save_json_file("briefings.json", briefings or [])
    logger.info(f"브리핑 저장: {path}")
    return path


def get_previous_snapshot():
    # briefings.json의 마지막 레코드 = 가장 최근에 저장된 실행 결과.
    # (오늘 아침 실행이라면 어제 16:00 종가, 오늘 16:00 실행이라면 오늘 07:30 값)
    try:
        briefings = load_briefings()
        if briefings:
            return briefings[-1]
    except Exception as e:
        logger.warning(f"이전 스냅샷 조회 실패 (최초 실행이거나 기록 없음): {e}")
    return None


def recompute_pct_vs_previous(numeric_data, pct_data, portfolio_map):
    prev = get_previous_snapshot()
    if not prev:
        logger.info("이전 스냅샷 없음 - 이번 1회만 Yahoo 자체 pct 값을 그대로 사용")
        return pct_data, portfolio_map

    prev_metrics = prev.get("metrics", {})
    new_pct_data = dict(pct_data)
    for key in list(pct_data.keys()):
        curr_val = numeric_data.get(key)
        prev_val = prev_metrics.get(key)
        if curr_val is not None and prev_val:
            new_pct_data[key] = (curr_val - prev_val) / prev_val * 100
        # else: 이전 기록에 없는 키(신규 지표 등)는 Yahoo 자체 pct를 그대로 둠

    prev_portfolio = prev.get("portfolio", {})
    new_portfolio_map = {}
    for sym, info in portfolio_map.items():
        new_info = dict(info)
        curr_val = info.get("price")
        prev_info = prev_portfolio.get(sym)
        prev_val = prev_info.get("price") if prev_info else None
        if curr_val is not None and prev_val:
            new_info["change_rate"] = (curr_val - prev_val) / prev_val * 100
        # else: 이전 기록에 없는 종목(예: 방금 추가한 BOTZ 최초 실행)은
        # get_stock_price_any가 계산한 값을 그대로 둠
        new_portfolio_map[sym] = new_info

    return new_pct_data, new_portfolio_map


def build_market_text(numeric_data, pct_data):
    # recompute_pct_vs_previous() 이후 pct_data가 바뀌었으므로, Gemini에게
    # 넘길 텍스트도 이 최신 pct 기준으로 다시 조립함 (원래 get_market_data가
    # 만든 텍스트는 Yahoo 자체 pct가 이미 박혀 있어서 재사용 불가).
    lines = []
    for sym, name, key in MARKET_ITEMS:
        val = numeric_data.get(key)
        pct = pct_data.get(key)
        if val is None:
            lines.append(f"• {name}: 데이터 수집 지연")
            continue
        unit = "%" if "금리" in name else ""
        is_usd = (".KS" not in sym and "KRW" not in sym and "코스피" not in name and "금리" not in name)
        currency_symbol = "$" if is_usd else ""
        pct_str = f" ({pct:+.2f}%)" if pct is not None else ""
        lines.append(f"• {name}: {currency_symbol}{val:,.2f}{unit}{pct_str}")
        if key == "kospi" and numeric_data.get("btc") is not None:
            lines.append(f"• 비트코인 (원화): {numeric_data['btc']:,.2f} ({pct_data.get('btc', 0):+.2f}%)")
        elif key == "gold_intl" and numeric_data.get("gold_kr") is not None:
            lines.append(f"• 국내 금 (1g): {numeric_data['gold_kr']:,.2f} ({pct_data.get('gold_kr', 0):+.2f}%)")
    return "\n".join(lines)


def build_portfolio_text(portfolio_map):
    lines = []
    for sym, name in MY_PORTFOLIO_TICKERS:
        info = portfolio_map.get(sym)
        if not info or info.get("price") is None:
            lines.append(f"• {name}: 데이터 수집 지연")
            continue
        price = info["price"]
        pct = info.get("change_rate", 0.0) or 0.0
        is_domestic = sym.endswith(".KS")
        currency_symbol = "" if is_domestic else "$"
        lines.append(f"• {name}: {currency_symbol}{price:,.2f} ({pct:+.2f}%)")
    return "\n".join(lines)


# ==========================================
# 4. 로컬 누적 저장 (briefings.json — 구 history.json에서 개명)
# ==========================================

def save_to_s3(numeric_data, pct_data, portfolio_map, oil_data, fear_score, news_list, reasons_dict):
    # 하위 호환 함수명 — 실제로는 저장소의 briefings.json에 기록
    briefings = load_briefings()

    kst_now = now_kst()
    today_str = kst_now.strftime("%Y-%m-%d")

    # ⚠️ 핵심 버그 수정: 이번 실행의 Gemini 분석이 실패해서 reasons_dict가
    # 비어있는데, 오늘 이미 다른 실행(예: 아침 07:30)이 멀쩡한 분석을
    # 저장해뒀다면, 그 멀쩡한 걸 빈 값으로 덮어쓰지 않고 그대로 보존한다.
    # (예전엔 07:30에 성공해도 16:00이 실패하면 하루치가 통째로 날아갔음)
    used_fallback_reasons = False
    if not reasons_dict:
        existing_today = next((h for h in briefings if h.get("date") == today_str), None)
        if existing_today and existing_today.get("reasons"):
            logger.warning("이번 실행 Gemini 분석 실패 - 오늘 기존에 저장된 분석 결과를 유지합니다.")
            reasons_dict = existing_today["reasons"]
            used_fallback_reasons = True

    record = {
        "date": today_str,
        "metrics": {
            "usdkrw": numeric_data.get("usdkrw"),
            "usdkrw_pct": pct_data.get("usdkrw"),
            "kospi": numeric_data.get("kospi"),
            "kospi_pct": pct_data.get("kospi"),
            "nasdaq": numeric_data.get("nasdaq"),
            "nasdaq_pct": pct_data.get("nasdaq"),
            "sp500": numeric_data.get("sp500"),
            "sp500_pct": pct_data.get("sp500"),
            "us10y": numeric_data.get("us10y"),
            "wti": numeric_data.get("wti"),
            "wti_pct": pct_data.get("wti"),
            "gold_intl": numeric_data.get("gold_intl"),
            "gold_intl_pct": pct_data.get("gold_intl"),
            "gold_kr": numeric_data.get("gold_kr"),
            "gold_kr_pct": pct_data.get("gold_kr"),
            "btc": numeric_data.get("btc"),
            "btc_pct": pct_data.get("btc"),
            "gasoline": oil_data.get("gasoline"),
            "premium_gasoline": oil_data.get("premium_gasoline"),
            "diesel": oil_data.get("diesel"),
            "fear_score": fear_score
        },
        "portfolio": portfolio_map,
        "news": news_list,
        "reasons": reasons_dict or {},
        "reason": (reasons_dict or {}).get("overall", "시장 동향 분석 중")
    }

    briefings = [h for h in briefings if h.get("date") != today_str]
    briefings.append(record)

    if len(briefings) > 180:
        briefings = briefings[-180:]

    save_briefings(briefings)
    logger.info("briefings.json 저장 완료")
    return used_fallback_reasons

def send_slack(text):
    webhook_url = clean_str(os.environ.get("SLACK_WEBHOOK_URL", ""))
    if not webhook_url:
        raise ValueError("SLACK_WEBHOOK_URL이 없습니다.")
    http_post_json(webhook_url, {"text": text}, timeout=10)


# ==========================================
# 5. 주간 리포트 (토요일 07:30 KST)
# ==========================================
# 주말엔 국내/미국 장이 다 안 열리므로, 토요일 아침 시점에 이미 그 주(월~금)
# 데이터가 완결되어 있음. 새로운 시세 수집은 하지 않고 briefings.json에 이미
# 쌓인 일별 기록을 집계만 함. 숫자는 전부 코드가 계산하고, Gemini는 "원인
# 서술"과 "차주 예정 이벤트 사실 나열"만 맡음 (일간 브리핑과 동일한 원칙).


# 주간 집계 대상 키 (macro numeric_data 키 -> 표시용 라벨/통화/단위)
WEEKLY_MACRO_SPECS = [
    ("wti", "WTI", "$", "", 2),
    ("gold_intl", "국제 금", "$", "", 1),
    ("kospi", "코스피", "", "", 1),
    ("nasdaq", "나스닥", "", "", 1),
    ("us10y", "미 국채금리(10Y)", "", "%", 2),
]


def get_full_briefings():
    try:
        return load_briefings()
    except Exception as e:
        logger.error(f"briefings.json 조회 실패: {e}")
        return []


# 하위 호환 별칭
def get_full_history():
    return get_full_briefings()


def get_weekday_records(count=10):
    # 토/일 기록(있다면)은 제외하고, 월~금 평일 기록만 최근 순으로 최대
    # count개 반환. 이번 주 5개 + 전주 5개를 한 번에 가져오는 용도.
    history = get_full_history()
    weekday_records = []
    for h in history:
        try:
            d = datetime.datetime.strptime(h["date"], "%Y-%m-%d")
        except Exception:
            continue
        if d.weekday() < 5:  # 0=월 ... 4=금
            weekday_records.append(h)
    weekday_records.sort(key=lambda h: h["date"])
    return weekday_records[-count:]


def compute_weekly_change(this_week, last_week, get_value_fn):
    # this_week/last_week: 레코드 리스트. get_value_fn(record) -> 숫자|None
    if not this_week:
        return None
    start_val = get_value_fn(this_week[0])
    end_val = get_value_fn(this_week[-1])
    if start_val is None or end_val is None or start_val == 0:
        return {"start": start_val, "end": end_val, "pct_this_week": None, "pct_vs_last_week": None}

    pct_this_week = (end_val - start_val) / start_val * 100

    pct_vs_last_week = None
    if last_week:
        last_week_end = get_value_fn(last_week[-1])
        if last_week_end:
            pct_vs_last_week = (end_val - last_week_end) / last_week_end * 100

    return {"start": start_val, "end": end_val, "pct_this_week": pct_this_week, "pct_vs_last_week": pct_vs_last_week}


def compute_weekly_metrics(this_week, last_week):
    macro = {}
    for key, label, currency, unit, decimals in WEEKLY_MACRO_SPECS:
        macro[key] = compute_weekly_change(this_week, last_week, lambda h, k=key: h.get("metrics", {}).get(k))

    portfolio = {}
    for sym, name in MY_PORTFOLIO_TICKERS:
        portfolio[sym] = compute_weekly_change(
            this_week, last_week,
            lambda h, s=sym: (h.get("portfolio", {}).get(s) or {}).get("price")
        )

    fear_scores = []
    for h in this_week:
        score = h.get("metrics", {}).get("fear_score")
        try:
            d = datetime.datetime.strptime(h["date"], "%Y-%m-%d")
            day_label = ["월", "화", "수", "목", "금"][d.weekday()]
        except Exception:
            day_label = "?"
        fear_scores.append((day_label, score))

    return macro, portfolio, fear_scores


def find_biggest_mover(portfolio_metrics):
    # 이번 주 등락률(절대값) 기준으로 가장 크게 움직인 종목 (히어로용)
    best_sym, best_change = None, None
    for sym, name in MY_PORTFOLIO_TICKERS:
        m = portfolio_metrics.get(sym)
        if not m or m.get("pct_this_week") is None:
            continue
        if best_change is None or abs(m["pct_this_week"]) > abs(best_change):
            best_sym, best_change = sym, m["pct_this_week"]
    return best_sym, best_change


def search_stock_news(query, max_items=5):
    # Google News RSS 키워드 검색 - API 키 발급 없이 바로 사용 가능.
    # 우리가 지정한 종목명으로 직접 검색하는 것이므로, "AI가 알아서 검색"이
    # 아니라 "우리가 지정한 소스에서 가져온다"는 원칙에 부합함.
    try:
        url = f"https://news.google.com/rss/search?q={urllib.parse.quote(query)}&hl=ko&gl=KR&ceid=KR:ko"
        xml_data = http_get(url, timeout=6)
        root = ET.fromstring(xml_data)
        items = root.findall('.//item')[:max_items]
        results = []
        for item in items:
            title_el = item.find('title')
            pubdate_el = item.find('pubDate')
            if title_el is not None and title_el.text:
                results.append({
                    "title": title_el.text.strip(),
                    "publishedAt": pubdate_el.text.strip() if pubdate_el is not None and pubdate_el.text else None,
                })
        return results
    except Exception as e:
        logger.warning(f"'{query}' 뉴스 검색 실패: {e}")
        return []


def get_period_ai_analysis(macro_metrics, portfolio_metrics, period_news_titles, per_symbol_news,
                            period_word="주", next_period_word="차주"):
    # period_word: "주" 또는 "달" / next_period_word: "차주" 또는 "차월"
    # (주간/월간 리포트가 이 함수 하나를 공유함 - 로직은 동일하고 문구만 다름)
    api_key = clean_str(os.environ.get("GEMINI_API_KEY", ""))
    if not api_key:
        logger.error(f"❌ GEMINI_API_KEY 없음 - {period_word}간 AI 분석 생략")
        return None, None

    # 등락률 요약 텍스트는 전부 코드가 계산한 값만 사용 (숫자 할루시네이션 방지 원칙 동일 적용)
    macro_lines = []
    for key, label, currency, unit, decimals in WEEKLY_MACRO_SPECS:
        m = macro_metrics.get(key)
        if m and m.get("pct_this_week") is not None:
            macro_lines.append(f"• {label}: {_fmt_num(m['start'], decimals)}{unit} → {_fmt_num(m['end'], decimals)}{unit} (이번 {period_word} {_pct_str(m['pct_this_week'])})")
    macro_text = "\n".join(macro_lines)

    portfolio_lines = []
    for sym, name in MY_PORTFOLIO_TICKERS:
        m = portfolio_metrics.get(sym)
        if m and m.get("pct_this_week") is not None:
            portfolio_lines.append(f"• {name}: {_fmt_num(m['start'], 2)} → {_fmt_num(m['end'], 2)} (이번 {period_word} {_pct_str(m['pct_this_week'])})")
    portfolio_text = "\n".join(portfolio_lines)

    news_text = "\n".join([f"• {t}" for t in period_news_titles])

    per_symbol_news_text_parts = []
    for sym, name in MY_PORTFOLIO_TICKERS:
        items = per_symbol_news.get(sym, [])
        if items:
            lines = "\n".join([f"  - {it['title']} ({it.get('publishedAt', '날짜 미상')})" for it in items])
            per_symbol_news_text_parts.append(f"[{name}]\n{lines}")
    per_symbol_news_text = "\n\n".join(per_symbol_news_text_parts) if per_symbol_news_text_parts else "(수집된 종목별 뉴스 없음)"

    prompt = f"""
당신은 여의도 수석 스트래티지스트입니다. 아래는 이번 {period_word}(평일 기준) 시장 데이터와 뉴스입니다.

[매우 중요 - 숫자 금지]
- 등락률/가격 수치는 이미 위에 정확히 제공되어 있습니다. 본문에 새로운 숫자를 만들어 쓰지 말고, 원인과 영향만 서술하세요.

[매우 중요 - {next_period_word} 이벤트는 사실만]
- "next_period_events"의 각 종목 값은, 아래 [종목별 뉴스]에 실제로 명시된 날짜/일정이 있을 때만 채우세요.
- 없으면 반드시 "확인된 예정 이벤트 없음"이라고 쓰세요. 추측하거나 지어내지 마세요.
- 이벤트를 적을 땐 "어디에 어떤 종류의 영향(변동성 확대, 관련 종목 파급 등)"만 언급하고, 주가가 오를지 내릴지는 절대 판단하지 마세요.

반드시 마크다운 없이 순수 JSON으로만 출력하세요.

JSON 포맷:
{{
  "issue_analysis": "이번 {period_word} 가장 임팩트 컸던 이슈 1~2개의 원인과 파급 영향을 3~4문장으로",
  "next_period_events": {{
    "NVDA": "{next_period_word} 이벤트 사실 + 영향 범위, 또는 '확인된 예정 이벤트 없음'",
    "AAPL": "...",
    "TSLA": "...",
    "005930.KS": "...",
    "MSFT": "...",
    "SPCX": "...",
    "BOTZ": "..."
  }}
}}

[이번 {period_word} 거시/자원 지표]
{macro_text}

[이번 {period_word} 보유·관심 종목]
{portfolio_text}

[이번 {period_word} 수집된 일반 뉴스 헤드라인]
{news_text}

[종목별 뉴스 검색 결과]
{per_symbol_news_text}
"""
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"thinkingConfig": {"thinkingLevel": "low"}}
    }
    headers = {"x-goog-api-key": api_key}

    return call_gemini_json(payload, headers, timeout=GEMINI_HTTP_TIMEOUT, context_label=f"{period_word}간 AI 분석")


# 하위 호환용 별칭 (기존 코드에서 get_weekly_ai_analysis로 호출하던 부분)
def get_weekly_ai_analysis(macro_metrics, portfolio_metrics, weekly_news_titles, per_symbol_news):
    result, model = get_period_ai_analysis(macro_metrics, portfolio_metrics, weekly_news_titles, per_symbol_news,
                                            period_word="주", next_period_word="차주")
    if result and "next_period_events" in result:
        result["next_week_events"] = result.pop("next_period_events")
    return result, model


KRX_HOLIDAYS_2026 = {
    "2026-01-01", "2026-02-16", "2026-02-17", "2026-02-18",
    "2026-03-02", "2026-03-03", "2026-05-05", "2026-05-24",
    "2026-06-06", "2026-08-15", "2026-09-24", "2026-09-25",
    "2026-09-26", "2026-10-03", "2026-10-09", "2026-12-25", "2026-12-31",
}
# ⚠️ 매년 갱신 필요 (한국거래소 공식 휴장일 공지 기준)


def get_korean_holidays_next_week():
    # 다음 주 국내 증시 휴장일 - 사실(고정 달력) 기반
    kst_now = now_kst()
    next_monday = kst_now + datetime.timedelta(days=(7 - kst_now.weekday()))
    holidays = []
    for i in range(5):
        d = next_monday + datetime.timedelta(days=i)
        if d.strftime("%Y-%m-%d") in KRX_HOLIDAYS_2026:
            holidays.append(d.strftime("%m/%d(") + ["월", "화", "수", "목", "금"][i] + ")")
    return holidays


def get_korean_holidays_in_month(year, month):
    # 특정 월 전체의 국내 증시 휴장일 (월간 리포트의 "차월 체크"용)
    holidays = []
    d = datetime.date(year, month, 1)
    while d.month == month:
        if d.strftime("%Y-%m-%d") in KRX_HOLIDAYS_2026:
            weekday_kr = ["월", "화", "수", "목", "금", "토", "일"][d.weekday()]
            holidays.append(d.strftime(f"%m/%d({weekday_kr})"))
        d += datetime.timedelta(days=1)
    return holidays


def build_period_report_html(period_label, date_range, macro_metrics, portfolio_metrics,
                              fear_scores, issue_analysis, next_period_events, biggest_mover_text,
                              holiday_text, title_word="주간", period_word="주", next_period_word="차주"):
    # period_label: "2026-W36" 또는 "2026-09" 같은 리포트 식별자 (S3 키/타이틀용)
    # title_word: "주간"/"월간", period_word: "주"/"달", next_period_word: "차주"/"차월"
    def row(label, m, currency="", unit="", decimals=1):
        if not m or m.get("pct_this_week") is None:
            return f'<tr><td class="label">{label}</td><td colspan="2" class="muted">데이터 부족</td></tr>'
        this_cls = "up" if m["pct_this_week"] >= 0 else "down"
        vs_html = "N/A"
        vs_cls = "muted"
        if m.get("pct_vs_last_week") is not None:
            vs_cls = "up" if m["pct_vs_last_week"] >= 0 else "down"
            vs_html = f'{m["pct_vs_last_week"]:+.2f}{"%p" if unit == "%" else "%"}'
        return f'''<tr>
          <td class="label">{label}</td>
          <td class="num">{currency}{_fmt_num(m["start"], decimals)}{unit} → {currency}{_fmt_num(m["end"], decimals)}{unit}
            <span class="{this_cls}">({m["pct_this_week"]:+.2f}{"%p" if unit == "%" else "%"})</span></td>
          <td class="num {vs_cls}">{vs_html}</td>
        </tr>'''

    macro_rows = "".join([row(label, macro_metrics.get(key), currency, unit, decimals)
                           for key, label, currency, unit, decimals in WEEKLY_MACRO_SPECS])

    portfolio_rows_parts = []
    for sym, name in MY_PORTFOLIO_TICKERS:
        is_domestic = sym.endswith(".KS")
        currency = "" if is_domestic else "$"
        unit = "원" if is_domestic else ""
        portfolio_rows_parts.append(row(name, portfolio_metrics.get(sym), currency, unit, 2))
    portfolio_rows = "".join(portfolio_rows_parts)

    fear_avg = None
    valid_scores = [s for _, s in fear_scores if s is not None]
    if valid_scores:
        fear_avg = sum(valid_scores) / len(valid_scores)
    fear_labels = json.dumps([d for d, _ in fear_scores], ensure_ascii=False)
    fear_values = json.dumps([s for _, s in fear_scores])

    events_html_parts = []
    for sym, name in MY_PORTFOLIO_TICKERS:
        text = (next_period_events or {}).get(sym, "확인된 예정 이벤트 없음")
        is_none = "없음" in text
        name_style = 'style="color:#475569;"' if is_none else ''
        events_html_parts.append(f'''
        <div class="event">
          <div class="name" {name_style}>{name}</div>
          <div class="impact">{text}</div>
        </div>''')
    events_html = "".join(events_html_parts)

    prev_period_word = "전주" if period_word == "주" else "전월"

    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title_word} 마켓 리포트 · {date_range}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:#0b1120; color:#e2e8f0; font-family:'Pretendard',-apple-system,'Malgun Gothic',sans-serif; -webkit-font-smoothing:antialiased; }}
  .num {{ font-variant-numeric: tabular-nums; }}
  .wrap {{ max-width:720px; margin:0 auto; padding:0 20px 60px; }}
  .hero {{ padding:56px 0 40px; border-bottom:1px solid #1e293b; }}
  .hero .eyebrow {{ color:#64748b; font-size:13px; margin-bottom:10px; }}
  .hero .headline {{ font-size:34px; font-weight:800; line-height:1.3; letter-spacing:-0.5px; color:#f8fafc; }}
  section {{ padding:36px 0; border-bottom:1px solid #1e293b; }}
  section:last-of-type {{ border-bottom:none; }}
  h2 {{ font-size:15px; font-weight:700; color:#f1f5f9; margin:0 0 20px; }}
  h3 {{ font-size:12px; font-weight:600; color:#64748b; margin:24px 0 10px; }}
  h3:first-of-type {{ margin-top:0; }}
  table {{ width:100%; border-collapse:collapse; font-size:13.5px; }}
  th {{ text-align:right; color:#475569; font-weight:500; font-size:11px; padding:0 0 8px; }}
  th:first-child {{ text-align:left; }}
  td {{ padding:9px 0; border-top:1px solid #1e293b; }}
  td:not(:first-child) {{ text-align:right; }}
  .label {{ color:#cbd5e1; }}
  .up {{ color:#4ade80; }} .down {{ color:#f87171; }} .muted {{ color:#64748b; }}
  .issue p {{ font-size:14.5px; line-height:1.85; color:#cbd5e1; margin:0; white-space:pre-line; }}
  .event {{ padding:14px 0; border-top:1px solid #1e293b; }}
  .event:first-of-type {{ border-top:none; }}
  .event .name {{ font-size:14px; color:#f1f5f9; font-weight:600; }}
  .event .impact {{ font-size:12.5px; color:#64748b; margin-top:4px; }}
  .holiday-note {{ background:#131b2e; border:1px solid #1e293b; border-radius:10px; padding:12px 16px; font-size:13px; color:#cbd5e1; margin-bottom:22px; }}
  .chart-box {{ height:180px; margin-top:4px; }}
  footer {{ padding:28px 0 8px; }}
  footer p {{ font-size:11.5px; color:#475569; line-height:1.6; margin:0 0 14px; }}
  footer a {{ color:#38bdf8; text-decoration:none; font-size:13px; }}
</style></head>
<body><div class="wrap">
  <div class="hero">
    <div class="eyebrow">{title_word} 마켓 리포트 · {date_range}</div>
    <div class="headline">{biggest_mover_text}</div>
  </div>
  <section>
    <h2>📈 이번 {period_word} 흐름</h2>
    <h3>거시 · 자원 · 금리</h3>
    <table><tr><th>지표</th><th>이번 {period_word}</th><th>{prev_period_word} 대비</th></tr>{macro_rows}</table>
    <h3>보유 · 관심 종목</h3>
    <table><tr><th>종목</th><th>이번 {period_word}</th><th>{prev_period_word} 대비</th></tr>{portfolio_rows}</table>
    <h3>심리지수 (공포·탐욕)</h3>
    <div class="chart-box"><canvas id="fearGreedChart"></canvas></div>
    <p style="font-size:13px; color:#cbd5e1; margin-top:12px;">
      이번 {period_word} 평균 <strong style="color:#f1f5f9;">{f"{fear_avg:.0f}점" if fear_avg is not None else "N/A"}</strong>
    </p>
  </section>
  <section class="issue">
    <h2>🔍 이번 {period_word} 이슈 분석</h2>
    <p>{issue_analysis or "분석 데이터를 생성하지 못했습니다."}</p>
  </section>
  <section>
    <h2>📆 {next_period_word} 체크</h2>
    <div class="holiday-note">🗓️ {next_period_word} 국내 증시 휴장일: <strong>{holiday_text}</strong></div>
    {events_html}
  </section>
  <footer>
    <p>※ {next_period_word} 체크는 수집된 뉴스 기준이며, 언론에 보도되지 않은 일정은 포함되지 않을 수 있습니다.</p>
  </footer>
</div>
<script>
  const ctx = document.getElementById('fearGreedChart').getContext('2d');
  new Chart(ctx, {{
    type: 'line',
    data: {{ labels: {fear_labels}, datasets: [{{
      data: {fear_values}, borderColor: '#38bdf8', backgroundColor: '#38bdf822',
      borderWidth: 2.5, fill: true, tension: 0.3, pointRadius: 4,
      pointBackgroundColor: (c) => {{ const v = c.raw; if (v>=75) return '#22c55e'; if (v>=55) return '#4ade80'; if (v>=45) return '#eab308'; if (v>=25) return '#f97316'; return '#ef4444'; }},
      pointBorderColor: '#0b1120', pointBorderWidth: 2
    }}]}},
    options: {{ responsive:true, maintainAspectRatio:false, plugins:{{legend:{{display:false}}}},
      scales: {{ y: {{min:0,max:100,grid:{{color:'#1e293b'}},ticks:{{color:'#64748b',stepSize:25}}}}, x:{{grid:{{display:false}},ticks:{{color:'#64748b'}}}} }} }}
  }});
</script>
</body></html>"""


# 하위 호환용 별칭
def build_weekly_report_html(week_label, date_range, macro_metrics, portfolio_metrics,
                              fear_scores, issue_analysis, next_week_events, biggest_mover_text):
    holidays = get_korean_holidays_next_week()
    holiday_text = ", ".join(holidays) if holidays else "없음 (정상 5거래일)"
    return build_period_report_html(week_label, date_range, macro_metrics, portfolio_metrics,
                                     fear_scores, issue_analysis, next_week_events, biggest_mover_text,
                                     holiday_text, title_word="주간", period_word="주", next_period_word="차주")


def build_period_slack_message(title_word, period_word, next_period_word, date_range, report_url,
                                biggest_mover_text, fear_avg, macro_metrics, next_period_events_preview):
    kospi = macro_metrics.get("kospi") or {}
    nasdaq = macro_metrics.get("nasdaq") or {}
    kospi_pct = kospi.get("pct_this_week")
    nasdaq_pct = nasdaq.get("pct_this_week")

    fear_line = f"{fear_avg:.0f}점" if fear_avg is not None else "N/A"
    kospi_line = f"{kospi_pct:+.2f}%" if kospi_pct is not None else "N/A"
    nasdaq_line = f"{nasdaq_pct:+.2f}%" if nasdaq_pct is not None else "N/A"

    preview_lines = [f"• {text}" for _, text in next_period_events_preview[:2]]
    remaining = len(next_period_events_preview) - 2
    if remaining > 0:
        preview_lines.append(f"_외 {remaining}건 · 전체 리포트에서 확인_")
    preview_text = "\n".join(preview_lines) if preview_lines else "확인된 예정 이벤트 없음"

    return f"""📅 *{title_word} 마켓 리포트* ({date_range})

💡 {biggest_mover_text}

📊 *핵심 지표*
• 😐 심리지수 평균: {fear_line}
• 📈 코스피: {kospi_line}
• 📉 나스닥: {nasdaq_line}

📆 *{next_period_word} 체크*
{preview_text}

🔗 <{report_url}|전체 리포트 보기 (차트·상세 흐름·이슈 분석)>"""


# 하위 호환용 별칭
def build_weekly_slack_message(date_range, report_url, biggest_mover_text,
                                fear_avg, macro_metrics, next_week_events_preview):
    return build_period_slack_message("주간", "주", "차주", date_range, report_url,
                                       biggest_mover_text, fear_avg, macro_metrics, next_week_events_preview)


def run_period_report(this_period, last_period, period_label, report_key, holiday_text,
                       title_word="주간", period_word="주", next_period_word="차주"):
    # 주간/월간 리포트가 공유하는 핵심 로직. this_period/last_period는 각각
    # "이번 기간"/"직전 기간"에 해당하는 briefings.json 레코드 리스트.
    macro_metrics, portfolio_metrics, fear_scores = compute_weekly_metrics(this_period, last_period)
    biggest_sym, biggest_pct = find_biggest_mover(portfolio_metrics)
    biggest_name = dict(MY_PORTFOLIO_TICKERS).get(biggest_sym, biggest_sym) if biggest_sym else "종목"
    period_particle = "는" if period_word == "주" else "은"  # "이번 주는" / "이번 달은"
    biggest_mover_text = (
        f"이번 {period_word}{period_particle} {biggest_name}가 {biggest_pct:+.2f}%로 가장 크게 흔들렸습니다"
        if biggest_sym else f"이번 {period_word}{period_particle} 데이터를 집계했습니다"
    )

    period_news_titles = []
    for h in this_period:
        for n in h.get("news", []):
            title = n.get("title") if isinstance(n, dict) else n
            if title:
                period_news_titles.append(title)

    per_symbol_news = {}
    for sym, name in MY_PORTFOLIO_TICKERS:
        per_symbol_news[sym] = search_stock_news(name)

    ai_result, model_used = get_period_ai_analysis(
        macro_metrics, portfolio_metrics, period_news_titles, per_symbol_news,
        period_word=period_word, next_period_word=next_period_word
    )
    issue_analysis = (ai_result or {}).get("issue_analysis", f"이슈 분석 데이터를 생성하지 못했습니다.")
    next_period_events = (ai_result or {}).get("next_period_events", {})

    date_range = f"{this_period[0]['date']} ~ {this_period[-1]['date']}"

    report_html = build_period_report_html(
        period_label, date_range, macro_metrics, portfolio_metrics,
        fear_scores, issue_analysis, next_period_events, biggest_mover_text,
        holiday_text, title_word=title_word, period_word=period_word, next_period_word=next_period_word
    )

    saved = save_text_file(report_key, report_html)
    report_url = dashboard_url(report_key)
    logger.info(f"{title_word} 리포트 저장 완료: {saved} ({report_url})")

    fear_avg = None
    valid_scores = [s for _, s in fear_scores if s is not None]
    if valid_scores:
        fear_avg = sum(valid_scores) / len(valid_scores)

    events_preview = [(sym, f"{dict(MY_PORTFOLIO_TICKERS).get(sym, sym)} — {text}")
                       for sym, text in next_period_events.items() if "없음" not in text]

    slack_message = build_period_slack_message(
        title_word, period_word, next_period_word, date_range, report_url,
        biggest_mover_text, fear_avg, macro_metrics, events_preview
    )

    try:
        send_slack(slack_message)
        logger.info(f"{title_word} 리포트 Slack 발송 완료")
    except Exception as e:
        logger.error(f"{title_word} 리포트 Slack 발송 실패: {e}")
        raise


def run_weekly_report():
    logger.info("주간 리포트 생성 시작")
    records = get_weekday_records(count=10)
    if len(records) < 2:
        logger.warning("주간 리포트: 데이터가 부족해서 생략 (최소 며칠치 필요)")
        return

    this_week = records[-5:] if len(records) >= 5 else records
    last_week = records[:-5] if len(records) > 5 else []

    kst_now = now_kst()
    week_label = f"{kst_now.year}-W{kst_now.isocalendar()[1]:02d}"

    holidays = get_korean_holidays_next_week()
    holiday_text = ", ".join(holidays) if holidays else "없음 (정상 5거래일)"

    run_period_report(this_week, last_week, week_label, f"reports/{week_label}.html", holiday_text,
                       title_word="주간", period_word="주", next_period_word="차주")


def get_month_records(year, month):
    # 특정 연/월에 속하는 평일(월~금) 기록만 날짜순으로 반환
    history = get_full_history()
    records = []
    for h in history:
        try:
            d = datetime.datetime.strptime(h["date"], "%Y-%m-%d")
        except Exception:
            continue
        if d.year == year and d.month == month and d.weekday() < 5:
            records.append(h)
    records.sort(key=lambda h: h["date"])
    return records


def run_monthly_report():
    # 매달 1일 07:30 KST에 실행된다고 가정 - 대상은 "지난달" 전체.
    logger.info("월간 리포트 생성 시작")
    kst_now = now_kst()
    first_of_this_month = kst_now.replace(day=1)
    last_month_end = first_of_this_month - datetime.timedelta(days=1)
    target_year, target_month = last_month_end.year, last_month_end.month

    if target_month == 1:
        prev_year, prev_month = target_year - 1, 12
    else:
        prev_year, prev_month = target_year, target_month - 1

    this_month = get_month_records(target_year, target_month)
    last_month = get_month_records(prev_year, prev_month)

    if len(this_month) < 2:
        logger.warning("월간 리포트: 지난달 데이터가 부족해서 생략")
        return

    month_label = f"{target_year}-{target_month:02d}"
    # 차월(target_month의 다음 달) 휴장일을 봐야 하므로 연도 넘어가는 경우도 처리
    next_month_year = target_year if target_month < 12 else target_year + 1
    next_month = target_month + 1 if target_month < 12 else 1
    holidays = get_korean_holidays_in_month(next_month_year, next_month)
    holiday_text = ", ".join(holidays) if holidays else "없음"

    run_period_report(this_month, last_month, f"{month_label}-monthly", f"reports/{month_label}-monthly.html",
                       holiday_text, title_word="월간", period_word="달", next_period_word="차월")



def _texts_from_briefing_record(record):
    """저장된 briefings 레코드에서 Gemini 프롬프트용 텍스트/맵을 재구성."""
    metrics = record.get("metrics") or {}
    portfolio_map = record.get("portfolio") or {}
    news_list = record.get("news") or []

    numeric_data = {}
    pct_data = {}
    for key in ("usdkrw", "kospi", "nasdaq", "sp500", "us10y", "wti", "gold_intl", "gold_kr", "btc", "copper"):
        if key in metrics:
            numeric_data[key] = metrics.get(key)
        pct_key = f"{key}_pct"
        if pct_key in metrics:
            pct_data[key] = metrics.get(pct_key)
        elif key in ("us10y",):
            pass

    oil_data = {
        "gasoline": metrics.get("gasoline"),
        "premium_gasoline": metrics.get("premium_gasoline"),
        "diesel": metrics.get("diesel"),
    }
    oil_diff = {"gasoline": 0.0, "premium_gasoline": 0.0, "diesel": 0.0}
    fear_score = metrics.get("fear_score", 50)

    market_text = build_market_text(numeric_data, pct_data)
    portfolio_text = build_portfolio_text(portfolio_map)
    oil_lines = []
    for k, label in (("gasoline", "전국 평균 일반휘발유"), ("premium_gasoline", "전국 평균 고급휘발유"), ("diesel", "전국 평균 자동차용경유")):
        v = oil_data.get(k)
        if v is not None:
            oil_lines.append(f"• {label}: {v:,.2f}원/L")
    oil_text = "\n".join(oil_lines) if oil_lines else "• 국내 유가: 데이터 없음"

    # news가 문자열 리스트인 구포맷 호환
    norm_news = []
    for n in news_list:
        if isinstance(n, dict):
            norm_news.append(n)
        elif isinstance(n, str):
            norm_news.append({"title": n, "url": None, "publishedAt": None})
    if not norm_news:
        norm_news = [{"title": "뉴스 없음", "url": None, "publishedAt": None}]

    return market_text, portfolio_text, oil_text, norm_news, numeric_data, pct_data, portfolio_map, oil_data, oil_diff, fear_score


def run_reanalyze_today():
    # 시세 재수집 없이, 오늘(또는 마지막) briefings 레코드의 AI reasons만 다시 생성.
    # 무료 쿼터를 아껴서 "시장 동향 분석 중"만 고쳐야 할 때 사용.
    logger.info("재분석 모드 시작 (시세 수집 생략, Gemini reasons만 갱신)")
    briefings = load_briefings()
    if not briefings:
        raise RuntimeError("briefings.json이 비어 있어 재분석할 수 없습니다.")

    date_str, _ = kst_date_str()
    idx = next((i for i, h in enumerate(briefings) if h.get("date") == date_str), None)
    if idx is None:
        idx = len(briefings) - 1
        date_str = briefings[idx].get("date")
        logger.warning(f"오늘({kst_date_str()[0]}) 레코드 없음 — 마지막 날짜 {date_str}를 재분석합니다.")

    record = briefings[idx]
    (market_text, portfolio_text, oil_text, news_list,
     numeric_data, pct_data, portfolio_map, oil_data, oil_diff, fear_score) = _texts_from_briefing_record(record)

    reasons_dict, model_used = get_itemized_ai_analysis(market_text, portfolio_text, oil_text, news_list)
    reasons_dict = build_prefixed_reasons(
        reasons_dict, numeric_data, pct_data, portfolio_map, oil_data, oil_diff
    )
    if not reasons_dict:
        raise RuntimeError(f"재분석 실패: Gemini reasons 비어 있음 (model={model_used})")

    # 같은 날짜 레코드의 reasons만 교체 저장
    record = dict(record)
    record["reasons"] = reasons_dict
    record["reason"] = reasons_dict.get("overall", record.get("reason", "시장 동향 분석 중"))
    briefings[idx] = record
    save_briefings(briefings)

    # history.json도 동기화 (대시보드 폴백용)
    try:
        save_json_file("history.json", briefings)
    except Exception as e:
        logger.warning(f"history.json 동기화 실패: {e}")

    try:
        save_new_data_structure(
            numeric_data, pct_data, portfolio_map, oil_data, fear_score,
            news_list, reasons_dict, "close", model_used
        )
    except Exception as e:
        logger.warning(f"계층 저장 실패(무시): {e}")

    logger.info(f"재분석 완료: date={date_str}, model={model_used}, fields={len(reasons_dict)}")
    return {"statusCode": 200, "body": f"Reanalyzed {date_str} with {model_used}"}


def lambda_handler(event, context):
    # ▼ GitHub Actions 스케줄 (Asia/Seoul, cron은 UTC):
    #   아침(월~토 07:30 KST): {"send_notification": true}
    #   장마감(월~금 16:00 KST): {"send_notification": false}
    #   주간(토요일 07:30 KST): {"mode": "weekly"}
    #   월간(매달 1일 07:30 KST): {"mode": "monthly"}
    #
    #   event가 None이거나 키가 없을 때(수동 테스트 등)는 기존 동작과 동일하게
    #   알림을 보내는 쪽을 기본값으로 둠 (안전한 기본값).
    event = event or {}

    # ▼ 재분석 모드: 시세 수집 없이 오늘 reasons만 다시 생성
    #   {"mode": "reanalyze"} 또는 CLI --mode reanalyze
    if event.get("mode") == "reanalyze":
        return run_reanalyze_today()

    # ▼ 주간 리포트 모드: 토요일 07:30 KST GitHub Actions workflow가
    #   {"mode": "weekly"}로 호출. 일간 브리핑(데이터 수집/알림)과 완전히
    #   분리된 별도 실행 경로 - 새 시세 수집 없이 briefings.json만 집계함.
    if event.get("mode") == "weekly":
        try:
            run_weekly_report()
            return {"statusCode": 200, "body": "Weekly report sent"}
        except Exception as e:
            logger.error(f"주간 리포트 실행 실패: {e}")
            try:
                send_slack(f"⚠️ 주간 리포트 실행 실패:\n```{str(e)}```")
            except Exception:
                pass
            raise e

    # ▼ 월간 리포트 모드: 매달 1일 07:30 KST GitHub Actions workflow가
    #   {"mode": "monthly"}로 호출. 지난달 전체(월~금)를 집계함.
    if event.get("mode") == "monthly":
        try:
            run_monthly_report()
            return {"statusCode": 200, "body": "Monthly report sent"}
        except Exception as e:
            logger.error(f"월간 리포트 실행 실패: {e}")
            try:
                send_slack(f"⚠️ 월간 리포트 실행 실패:\n```{str(e)}```")
            except Exception:
                pass
            raise e

    # ▼ 일간 브리핑 (GitHub Actions):
    #   아침(월~토 07:30 KST): {"send_notification": true}
    #   장마감(월~금 16:00 KST): {"send_notification": false}
    send_notification = bool(event.get("send_notification", True))

    logger.info(f"모닝 통합 브리핑 시작 (알림 발송: {send_notification})")
    try:
        # 날씨/시장지표/포트폴리오/공포지수/유가/뉴스 - 서로 의존관계가 없으므로
        # 순차 호출 대신 병렬로 돌려서 전체 대기시간을 크게 줄임.
        # (market_data와 portfolio_data는 내부적으로도 이미 병렬화되어 있음)
        with ThreadPoolExecutor(max_workers=6) as executor:
            f_weather = executor.submit(get_weather)
            f_market = executor.submit(get_market_data)
            f_portfolio = executor.submit(get_portfolio_data)
            f_fear = executor.submit(get_fear_and_greed)
            f_oil = executor.submit(get_gasoline_prices)
            f_news = executor.submit(get_news_headlines)

            weather_text, _ = f_weather.result()
            market_text, numeric_data, pct_data = f_market.result()
            portfolio_text, portfolio_map = f_portfolio.result()
            fear_text, fear_score = f_fear.result()
            oil_text, oil_data, oil_diff = f_oil.result()
            news_list = f_news.result()

        # ⚠️ pct 재계산: Yahoo가 주는 pct 대신, briefings.json의 "직전 실행 결과"와
        # 직접 비교해서 등락률을 다시 계산 (24시간 거래되는 환율/원자재의
        # Yahoo previousClose 기준점 불일치 버그 수정). market_text/portfolio_text도
        # 바뀐 pct 기준으로 다시 조립해서 Gemini에게 일관된 값을 전달함.
        pct_data, portfolio_map = recompute_pct_vs_previous(numeric_data, pct_data, portfolio_map)
        market_text = build_market_text(numeric_data, pct_data)
        portfolio_text = build_portfolio_text(portfolio_map)

        # Gemini AI 분석 호출 (알림을 안 보내는 실행이라도 대시보드용 데이터는
        # 최신으로 갱신되어야 하므로 동일하게 수행)
        reasons_dict, model_used = get_itemized_ai_analysis(market_text, portfolio_text, oil_text, news_list)

        # Gemini는 "원인/영향"만 서술했고, 등락 방향·%·가격은 우리 코드가
        # 직접 계산해서 각 항목 문장 앞에 붙임 (숫자 할루시네이션 원천 차단).
        reasons_dict = build_prefixed_reasons(
            reasons_dict, numeric_data, pct_data, portfolio_map, oil_data, oil_diff
        )

        # analysis_type: 07:30 알림 실행 = "morning", 16:00 조용히 갱신 실행 = "close"
        # (send_notification 플래그와 1:1로 대응하는 값이라 별도 event 필드 없이 유도)
        analysis_type = "morning" if send_notification else "close"

        # briefings.json 저장 - 두 실행 모두 동일하게 수행. 같은 날짜(date) 레코드는
        # save_to_s3 내부에서 덮어쓰기 처리되므로, 장마감 후 실행이 그날의
        # 최종 데이터를 한 번 더 정확하게 갱신해주는 효과가 있음.
        used_fallback_reasons = False
        try:
            used_fallback_reasons = save_to_s3(numeric_data, pct_data, portfolio_map, oil_data, fear_score, news_list, reasons_dict)
        except Exception as save_err:
            logger.error(f"briefings.json 저장 실패: {save_err}")

        # ⚠️ Gemini 실패는 silent(16:00)에서도 무조건 Slack 알림.
        # (시세는 저장됐지만 분석이 비면 대시보드가 빈칸으로 남음)
        if used_fallback_reasons:
            try:
                send_slack(
                    f"⚠️ {'아침' if send_notification else '장마감'} 실행: 시세는 최신화됐지만 "
                    f"AI 분석은 실패해서 기존 분석을 유지했습니다. (모델: {model_used or '알 수 없음'})"
                )
            except Exception:
                pass
        elif not reasons_dict:
            try:
                send_slack(
                    f"⚠️ {'아침' if send_notification else '장마감'} 실행: 시세는 저장됐지만 "
                    f"AI 분석이 비어 있습니다. (타임아웃/모델 오류 가능, 모델: {model_used or '알 수 없음'})"
                )
            except Exception:
                pass
            # Actions에서 초록 성공으로 위장하지 않음 (시세는 이미 저장됨)
            raise RuntimeError(
                f"AI 분석 결과가 비어 있습니다 (model={model_used or 'none'}). "
                f"Gemini 응답 검증 실패 또는 모든 모델 폴백 실패."
            )

        # 신규 계층 구조(raw/analysis/evidence/metadata) 저장 - 완전히 별도의
        # try/except로 감싸서, 여기서 실패해도 기존 briefings.json/Slack 흐름에는
        # 절대 영향이 없도록 함. 아직 실험적인 뼈대 단계이기 때문.
        try:
            save_new_data_structure(
                numeric_data, pct_data, portfolio_map, oil_data, fear_score,
                news_list, reasons_dict, analysis_type, model_used
            )
        except Exception as new_struct_err:
            logger.error(f"신규 데이터 구조 저장 실패 (기존 흐름엔 영향 없음): {new_struct_err}")

        if not send_notification:
            logger.info("send_notification=false: 데이터 갱신만 수행하고 Slack 알림은 생략합니다.")
            return {"statusCode": 200, "body": "Success (silent update, no notification)"}

        # Slack 지표 추출
        usdkrw = numeric_data.get('usdkrw', 0)
        usdkrw_p = pct_data.get('usdkrw', 0)
        kospi = numeric_data.get('kospi', 0)
        kospi_p = pct_data.get('kospi', 0)
        nasdaq = numeric_data.get('nasdaq', 0)
        nasdaq_p = pct_data.get('nasdaq', 0)
        sp500 = numeric_data.get('sp500', 0)
        sp500_p = pct_data.get('sp500', 0)
        btc = numeric_data.get('btc', 0)
        btc_p = pct_data.get('btc', 0)
        gold_intl = numeric_data.get('gold_intl', 0)
        gold_intl_p = pct_data.get('gold_intl', 0)
        gold_kr = numeric_data.get('gold_kr', 0)
        gold_kr_p = pct_data.get('gold_kr', 0)

        prem_price = oil_data.get('premium_gasoline') or 0.0
        prem_diff = oil_diff.get('premium_gasoline', 0.0)
        gas_price = oil_data.get('gasoline') or 0.0
        gas_diff = oil_diff.get('gasoline', 0.0)

        prem_sign = "+" if prem_diff > 0 else ""
        gas_sign = "+" if gas_diff > 0 else ""

        overall_summary = (reasons_dict or {}).get("overall", "시장 동향 분석 중입니다.")
        dash = site_base_url()
        dash_line = (
            f"🔗 <{dashboard_url()}|👉 항목별 개별 심층 사유 대시보드 열기>"
            if dash else
            "🔗 대시보드: `index.html` (GitHub Pages + `SITE_BASE_URL` 설정 시 링크 활성화)"
        )

        compact_briefing = f"""☀️ *모닝 퀵 브리핑* ({weather_text})

💡 *AI 핵심 시장 요약*
{overall_summary}

📊 *주요 지표 요약*
• 달러/원: {usdkrw:,.1f}원 ({usdkrw_p:+.2f}%) | 코스피: {kospi:,.1f} ({kospi_p:+.2f}%)
• 나스닥: {nasdaq:,.1f} ({nasdaq_p:+.2f}%) | S&P500: {sp500:,.1f} ({sp500_p:+.2f}%)
• 비트코인: {btc/100000000:,.2f}억 ({btc_p:+.2f}%)
• 🪙 국내 금(1g): {gold_kr:,.1f}원 ({gold_kr_p:+.2f}%) | 국제 금: ${gold_intl:,.1f} ({gold_intl_p:+.2f}%)
• ⛽ 고급유: {prem_price:,.1f}원 ({prem_sign}{prem_diff:,.2f}원) | 일반유: {gas_price:,.1f}원 ({gas_sign}{gas_diff:,.2f}원)
• 심리지수: {fear_text}

{dash_line}"""

        send_slack(compact_briefing)
        return {"statusCode": 200, "body": "Success"}
    except Exception as e:
        logger.error(f"실행 실패: {e}")
        # 에러 알림은 silent 모드 여부와 무관하게 항상 보냄
        # (조용히 도는 실행이 실패했는데 아무도 모르면 더 위험하므로).
        try:
            send_slack(f"⚠️ 모닝 브리핑 실행 실패 (send_notification={send_notification}):\n```{str(e)}```")
        except Exception:
            pass
        raise e


# ==========================================
# GitHub Actions / 로컬 CLI 진입점
# ==========================================
# Lambda 대신 GitHub Actions에서 직접 실행할 때 사용.
#   python lambda_function.py --mode daily --send-notification true
#   python lambda_function.py --mode daily --send-notification false
#   python lambda_function.py --mode weekly
#   python lambda_function.py --mode monthly


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Market briefing runner (GitHub Actions)")
    parser.add_argument(
        "--mode",
        choices=["daily", "weekly", "monthly", "reanalyze"],
        default="daily",
        help="daily=아침/장마감, weekly=주간, monthly=월간, reanalyze=오늘 AI만 재생성",
    )
    parser.add_argument(
        "--send-notification",
        choices=["true", "false"],
        default="true",
        help="daily 모드에서만 사용. false면 데이터 갱신만(장마감)",
    )
    args = parser.parse_args()

    if args.mode == "weekly":
        event = {"mode": "weekly"}
    elif args.mode == "monthly":
        event = {"mode": "monthly"}
    elif args.mode == "reanalyze":
        event = {"mode": "reanalyze"}
    else:
        event = {"send_notification": args.send_notification == "true"}

    result = lambda_handler(event, None)
    logger.info(f"실행 완료: {result}")
    return result


if __name__ == "__main__":
    main()
