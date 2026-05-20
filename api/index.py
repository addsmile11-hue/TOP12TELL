from flask import Flask, request, jsonify
import requests, os, re, datetime, json
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}
DAYS = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]
PROCESSED_UPDATES = []

# 디버그 모드: True로 두면 MTD 자리에 에러 원인을 노출
DEBUG_MTD = True


def fetch(url, encoding='euc-kr', timeout=5):
    res = requests.get(url, headers=HEADERS, timeout=timeout)
    res.encoding = encoding
    return BeautifulSoup(res.text, 'html.parser')


def get_last_trading_date():
    try:
        text = fetch("https://finance.naver.com/item/main.naver?code=005930", 'utf-8').select_one('em.date').get_text(strip=True)
        y, mo, d = map(int, re.search(r'(\d{4})\.(\d{2})\.(\d{2})', text).groups())
        dt = datetime.date(y, mo, d)
        return f"{y}년 {mo:02d}월 {d:02d}일 {DAYS[dt.weekday()]}"
    except Exception:
        t = datetime.date.today()
        return f"{t.year}년 {t.month:02d}월 {t.day:02d}일 {DAYS[t.weekday()]}"


def parse_naver_sise(url):
    try:
        table = fetch(url).select_one('table.type_2')
        if not table:
            return []
        stocks = []
        for row in table.select('tr'):
            tds = row.select('td')
            if len(tds) < 7:
                continue
            a = tds[1].select_one('a')
            tm = re.search(r'code=(\d+)', a.get('href', '')) if a else None
            if not tm:
                continue
            price = float(re.sub(r'[^\d]', '', tds[2].get_text(strip=True)) or 0)
            rate = float(re.sub(r'[^\d\.-]', '', tds[4].get_text(strip=True)) or 0)
            val = float(re.sub(r'[^\d]', '', tds[6].get_text(strip=True)) or 0)
            stocks.append({'ticker': tm.group(1), 'name': a.get_text(strip=True),
                           'price': price, 'rate': rate, 'value': val})
        return stocks
    except Exception:
        return []


def get_stock_fundamentals(ticker):
    try:
        soup = fetch(f"https://finance.naver.com/item/main.naver?code={ticker}", 'utf-8', timeout=5)
        ms = soup.select_one('#_market_sum')
        if ms:
            market_sum = re.sub(r'\s+', ' ', ms.get_text(strip=True)).replace('조 ', '조')
            if not market_sum.endswith('억'):
                market_sum += '억'
        else:
            market_sum = "N/A"
        per_elem = soup.select_one('#_cper') or soup.select_one('#_per')
        per = (per_elem.get_text(strip=True) if per_elem else "N/A") or "N/A"
        return market_sum, ("N/A" if per == '-' else per)
    except Exception:
        return "N/A", "N/A"


def get_prev_month_close(ticker, current_price):
    """
    여러 데이터 소스를 fallback 체인으로 시도해서 MTD(당월 상승률)를 가져옴.
    DEBUG_MTD=True인 경우 (mtd_value, debug_string)을 반환,
    DEBUG_MTD=False인 경우 mtd_value만 반환.
    """
    debug_info = []

    # === 시도 1: 네이버 siseJson API ===
    try:
        today = datetime.date.today()
        start = (today - datetime.timedelta(days=60)).strftime("%Y%m%d")
        end = today.strftime("%Y%m%d")
        url = (
            f"https://api.finance.naver.com/siseJson.naver?"
            f"symbol={ticker}&requestType=1&startTime={start}&endTime={end}&timeframe=day"
        )
        res = requests.get(url, headers=HEADERS, timeout=5)
        debug_info.append(f"NV-API:{res.status_code}")
        text = res.text.strip()
        
        if res.status_code == 200 and text and not text.startswith('Host'):
            # 응답은 JS literal 형식 (작은따옴표 사용)
            # JSON으로 변환: 작은따옴표 → 큰따옴표
            normalized = text.replace("'", '"')
            try:
                data = json.loads(normalized)
            except:
                import ast
                data = ast.literal_eval(text)
            
            if data and len(data) >= 2:
                rows = data[1:]
                prev_close = None
                for row in rows:
                    date_int = row[0]
                    close = row[4]
                    y = date_int // 10000
                    mo = (date_int // 100) % 100
                    if (y, mo) != (today.year, today.month):
                        prev_close = close
                    else:
                        break
                if prev_close and current_price:
                    rate = round((current_price - prev_close) / prev_close * 100, 2)
                    return (rate, "NV-API") if DEBUG_MTD else rate
                debug_info.append(f"no-prev(rows={len(rows)})")
            else:
                debug_info.append(f"empty-data")
        else:
            debug_info.append(f"body:{text[:30]}")
    except Exception as e:
        debug_info.append(f"NV-API-err:{type(e).__name__}")

    # === 시도 2: 네이버 sise_day.naver HTML 페이지 (Referer 헤더 포함) ===
    try:
        today = datetime.date.today()
        url = f"https://finance.naver.com/item/sise_day.naver?code={ticker}&page=1"
        headers = dict(HEADERS)
        headers["Referer"] = f"https://finance.naver.com/item/sise.naver?code={ticker}"
        res = requests.get(url, headers=headers, timeout=5)
        res.encoding = 'euc-kr'
        debug_info.append(f"NV-HTML:{res.status_code}")
        
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, 'html.parser')
            rows = soup.select('table.type2 tr')
            for row in rows:
                tds = row.select('td')
                if len(tds) < 2:
                    continue
                date_text = tds[0].get_text(strip=True)
                dm = re.match(r'(\d{4})\.(\d{2})\.(\d{2})', date_text)
                if not dm:
                    continue
                y, mo, d = map(int, dm.groups())
                if (y, mo) != (today.year, today.month):
                    close = float(re.sub(r'[^\d]', '', tds[1].get_text(strip=True)) or 0)
                    if close and current_price:
                        rate = round((current_price - close) / close * 100, 2)
                        return (rate, "NV-HTML") if DEBUG_MTD else rate
                    break
            debug_info.append(f"no-prev-html(rows={len(rows)})")
    except Exception as e:
        debug_info.append(f"NV-HTML-err:{type(e).__name__}")

    # === 시도 3: Yahoo Finance (한국 종목은 .KS 또는 .KQ) ===
    try:
        import time
        today = datetime.date.today()
        end_ts = int(time.mktime(today.timetuple()))
        start_ts = int(time.mktime((today - datetime.timedelta(days=60)).timetuple()))
        
        for suffix in ['KS', 'KQ']:  # 코스피, 코스닥 둘 다 시도
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}.{suffix}?period1={start_ts}&period2={end_ts}&interval=1d"
            try:
                res = requests.get(url, headers=HEADERS, timeout=5)
                if res.status_code != 200:
                    continue
                d = res.json()
                if not d.get('chart', {}).get('result'):
                    continue
                result = d['chart']['result'][0]
                timestamps = result.get('timestamp') or []
                closes = result.get('indicators', {}).get('quote', [{}])[0].get('close') or []
                
                prev_close = None
                for ts, close in zip(timestamps, closes):
                    if close is None:
                        continue
                    dt = datetime.date.fromtimestamp(ts)
                    if (dt.year, dt.month) != (today.year, today.month):
                        prev_close = close
                    else:
                        break
                if prev_close and current_price:
                    rate = round((current_price - prev_close) / prev_close * 100, 2)
                    return (rate, f"YH-{suffix}") if DEBUG_MTD else rate
            except Exception:
                continue
        debug_info.append("YH-fail")
    except Exception as e:
        debug_info.append(f"YH-err:{type(e).__name__}")

    err_str = "|".join(debug_info)[:50]
    return (None, err_str) if DEBUG_MTD else None


def pick_top_bottom(stocks_a, stocks_b):
    combined = sorted(stocks_a + stocks_b, key=lambda x: x['value'], reverse=True)[:50]
    combined.sort(key=lambda x: x['rate'], reverse=True)
    return {"up": combined[:3], "down": list(reversed(combined[-3:]))}


def get_stock_data():
    urls = {
        "k_cap": "https://finance.naver.com/sise/sise_market_sum.naver?sosok=0&page=1",
        "kd_cap": "https://finance.naver.com/sise/sise_market_sum.naver?sosok=1&page=1",
        "k_val": "https://finance.naver.com/sise/sise_quant.naver?rankingType=deal_value&sosok=0",
        "kd_val": "https://finance.naver.com/sise/sise_quant.naver?rankingType=deal_value&sosok=1",
    }

    with ThreadPoolExecutor(max_workers=5) as ex:
        date_f = ex.submit(get_last_trading_date)
        r = {k: ex.submit(parse_naver_sise, u) for k, u in urls.items()}
        results = {k: f.result() for k, f in r.items()}
        final_date = date_f.result()

    stock_dict = {
        "market_cap": pick_top_bottom(results["k_cap"], results["kd_cap"]),
        "trading_volume": pick_top_bottom(results["k_val"], results["kd_val"]),
    }

    flat = [s for g in stock_dict.values() for d in g.values() for s in d]

    with ThreadPoolExecutor(max_workers=12) as ex:
        funds = list(ex.map(lambda x: get_stock_fundamentals(x['ticker']), flat))
        monthly = list(ex.map(lambda x: get_prev_month_close(x['ticker'], x['price']), flat))

    for s, (ms, per), m_result in zip(flat, funds, monthly):
        s['market_sum'], s['per'] = ms, per
        if DEBUG_MTD:
            s['monthly_rate'], s['mtd_debug'] = m_result
        else:
            s['monthly_rate'] = m_result
            s['mtd_debug'] = None

    return stock_dict, final_date


def send_telegram(chat_id, text, markdown=False):
    payload = {"chat_id": chat_id, "text": text}
    if markdown:
        payload["parse_mode"] = "Markdown"
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json=payload)


def format_lines(stocks):
    lines = []
    for s in stocks:
        m = s.get('monthly_rate')
        if m is not None:
            m_str = f"{m:+.2f}%"
        elif DEBUG_MTD and s.get('mtd_debug'):
            m_str = f"N/A[{s['mtd_debug']}]"  # 디버그 정보 표시
        else:
            m_str = "N/A"
        lines.append(
            f"🔹 *{s['name']}* ({s['rate']}%)\n"
            f"Cap {s['market_sum']} f-PER {s['per']} MTD {m_str}"
        )
    return "\n".join(lines)


@app.route('/', methods=['POST', 'GET'])
def telegram_webhook():
    if request.method == 'GET':
        return "Stock Leader Screening Engine is running! [v3-debug]"

    update = request.get_json() or {}
    uid = update.get("update_id")
    if uid:
        if uid in PROCESSED_UPDATES:
            return jsonify({"status": "ignored_duplicate"})
        PROCESSED_UPDATES.append(uid)
        if len(PROCESSED_UPDATES) > 50:
            PROCESSED_UPDATES.pop(0)

    msg = update.get("message", {})
    text = msg.get("text")
    chat_id = msg.get("chat", {}).get("id")

    if text == '/check' and chat_id:
        send_telegram(chat_id, "🔄 당일 거래대금/시총 상위 종목을 스크리닝 중입니다. 잠시만 기다려주세요... [v3-debug]")
        data, date_str = get_stock_data()

        prompt = (
            "아래 12개의 종목에서 \n"
            "오늘 실적발표가 있었던 종목이 있는지 알려줘. \n"
            "오늘 \"최초\"와 관련된 종목이 있는지 알려줘. \n"
            "오늘 \"공시\"와 관련된 종목이 있는지 알려줘.\n\n\n\n"
        )
        body = (
            f"{prompt}"
            f"📋 *[{date_str}] 분석 대상 12개 종목 라인업 확정*\n\n"
            f"🏛️ *시가총액 상위 50위 그룹*\n\n"
            f"• 📈 상승 Top 3\n\n{format_lines(data['market_cap']['up'])}\n\n"
            f"• 📉 하락 Top 3\n\n{format_lines(data['market_cap']['down'])}\n\n"
            f"💸 *거래대금 상위 50위 그룹*\n\n"
            f"• 📈 상승 Top 3\n\n{format_lines(data['trading_volume']['up'])}\n\n"
            f"• 📉 하락 Top 3\n\n{format_lines(data['trading_volume']['down'])}"
        )
        send_telegram(chat_id, body, markdown=True)

    return jsonify({"status": "success"})
