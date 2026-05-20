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
    MTD(당월 상승률) 계산.
    
    로직:
    1. 네이버 siseJson API로 일봉 데이터 60일치 가져옴
    2. None 값이 있는 행은 안전하게 건너뜀
    3. 지난달 마지막 거래일 종가를 찾으면 그것 기준으로 계산
    4. 지난달 거래일이 없으면 (신규 상장주) → 상장일(가장 오래된 데이터) 기준으로 계산
    5. API 실패 시 HTML 페이지로 폴백
    """

    # === 시도 1: 네이버 siseJson API ===
    try:
        today = datetime.date.today()
        start = (today - datetime.timedelta(days=90)).strftime("%Y%m%d")
        end = today.strftime("%Y%m%d")
        url = (
            f"https://api.finance.naver.com/siseJson.naver?"
            f"symbol={ticker}&requestType=1&startTime={start}&endTime={end}&timeframe=day"
        )
        res = requests.get(url, headers=HEADERS, timeout=5)
        text = res.text.strip()

        if res.status_code == 200 and text and not text.startswith('Host'):
            normalized = text.replace("'", '"')
            try:
                data = json.loads(normalized)
            except Exception:
                import ast
                data = ast.literal_eval(text)

            if data and len(data) >= 2:
                rows = data[1:]
                prev_close = None        # 지난달 마지막 거래일 종가
                first_valid_close = None # 가장 오래된 거래일 종가 (신규 상장주 대비)

                for row in rows:
                    # 안전한 값 추출 - None 또는 잘못된 데이터 건너뛰기
                    if not row or len(row) < 5:
                        continue
                    date_int = row[0]
                    close = row[4]
                    if date_int is None or close is None:
                        continue
                    try:
                        date_int = int(date_int)
                        close = float(close)
                    except (TypeError, ValueError):
                        continue
                    if close <= 0:
                        continue

                    if first_valid_close is None:
                        first_valid_close = close

                    y = date_int // 10000
                    mo = (date_int // 100) % 100
                    if (y, mo) != (today.year, today.month):
                        prev_close = close

                # 지난달 데이터가 있으면 그것 기준
                base = prev_close if prev_close else first_valid_close
                if base and current_price:
                    return round((current_price - base) / base * 100, 2)
    except Exception:
        pass

    # === 시도 2: 네이버 sise_day.naver HTML 페이지 ===
    try:
        today = datetime.date.today()
        url = f"https://finance.naver.com/item/sise_day.naver?code={ticker}&page=1"
        headers = dict(HEADERS)
        headers["Referer"] = f"https://finance.naver.com/item/sise.naver?code={ticker}"
        res = requests.get(url, headers=headers, timeout=5)
        res.encoding = 'euc-kr'

        if res.status_code == 200:
            soup = BeautifulSoup(res.text, 'html.parser')
            rows = soup.select('table.type2 tr')

            # HTML은 최신순(현재→과거)으로 정렬됨
            # 모든 거래일 종가 수집 후, 지난달 마지막 거래일 또는 가장 오래된 거래일 사용
            entries = []  # [(date_tuple, close), ...]
            for row in rows:
                tds = row.select('td')
                if len(tds) < 2:
                    continue
                date_text = tds[0].get_text(strip=True)
                dm = re.match(r'(\d{4})\.(\d{2})\.(\d{2})', date_text)
                if not dm:
                    continue
                y, mo, d = map(int, dm.groups())
                close_text = tds[1].get_text(strip=True)
                close = float(re.sub(r'[^\d]', '', close_text) or 0)
                if close <= 0:
                    continue
                entries.append(((y, mo, d), close))

            if entries:
                # 지난달 마지막 거래일 찾기 (entries는 최신순이므로 첫 매칭이 정답)
                prev_close = None
                for (y, mo, d), close in entries:
                    if (y, mo) != (today.year, today.month):
                        prev_close = close
                        break

                # 없으면 가장 오래된 거래일 (entries의 마지막 = 신규 상장주의 상장일)
                base = prev_close if prev_close else entries[-1][1]
                if base and current_price:
                    return round((current_price - base) / base * 100, 2)
    except Exception:
        pass

    return None


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

    for s, (ms, per), m_rate in zip(flat, funds, monthly):
        s['market_sum'], s['per'], s['monthly_rate'] = ms, per, m_rate

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
        m_str = f"{m:+.2f}%" if m is not None else "N/A"
        lines.append(
            f"🔹 *{s['name']}* ({s['rate']}%)\n"
            f"Cap {s['market_sum']} f-PER {s['per']} MTD {m_str}"
        )
    return "\n".join(lines)


@app.route('/', methods=['POST', 'GET'])
def telegram_webhook():
    if request.method == 'GET':
        return "Stock Leader Screening Engine is running!"

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
        send_telegram(chat_id, "🔄 당일 거래대금/시총 상위 종목을 스크리닝 중입니다. 잠시만 기다려주세요...")
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
