from flask import Flask, request, jsonify
import requests, os, re, datetime
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
DAYS = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]
PROCESSED_UPDATES = []


def fetch(url, encoding='euc-kr', timeout=5):
    res = requests.get(url, headers=HEADERS, timeout=timeout)
    res.encoding = encoding
    return BeautifulSoup(res.text, 'html.parser')


def get_last_trading_date():
    """삼성전자(005930) 페이지의 em.date에서 마지막 거래일 추출"""
    try:
        text = fetch("https://finance.naver.com/item/main.naver?code=005930", 'utf-8').select_one('em.date').get_text(strip=True)
        y, mo, d = map(int, re.search(r'(\d{4})\.(\d{2})\.(\d{2})', text).groups())
        dt = datetime.date(y, mo, d)
        return f"{y}년 {mo:02d}월 {d:02d}일 {DAYS[dt.weekday()]}"
    except Exception:
        t = datetime.date.today()
        return f"{t.year}년 {t.month:02d}월 {t.day:02d}일 {DAYS[t.weekday()]}"


def parse_naver_sise(url):
    """시세 페이지에서 종목 리스트 파싱 (현재가도 같이 추출)"""
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
    """종목 상세 페이지에서 시가총액, PER 추출"""
    try:
        soup = fetch(f"https://finance.naver.com/item/main.naver?code={ticker}", 'utf-8', timeout=3)

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
    일별시세 페이지에서 지난달 마지막 거래일 종가를 찾아 월봉 상승률 계산.
    일별시세는 최신순으로 정렬되어 있으므로, 이번 달이 아닌 첫 행이 지난달 말일 종가.
    """
    try:
        today = datetime.date.today()
        soup = fetch(f"https://finance.naver.com/item/sise_day.naver?code={ticker}&page=1", 'euc-kr', timeout=3)
        for row in soup.select('table.type2 tr'):
            tds = row.select('td')
            if len(tds) < 2:
                continue
            date_text = tds[0].get_text(strip=True)
            dm = re.match(r'(\d{4})\.(\d{2})\.(\d{2})', date_text)
            if not dm:
                continue
            y, mo, d = map(int, dm.groups())
            # 오늘이 속한 달이 아닌 첫 거래일 = 지난달 마지막 거래일
            if (y, mo) != (today.year, today.month):
                close = float(re.sub(r'[^\d]', '', tds[1].get_text(strip=True)) or 0)
                if close and current_price:
                    return round((current_price - close) / close * 100, 2)
                return None
        return None
    except Exception:
        return None


def pick_top_bottom(stocks_a, stocks_b):
    """두 시장 합쳐 거래대금 상위 50 추출 후, 등락률 기준 상/하위 3개씩 반환"""
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

    # 펀더멘털 + 월봉 상승률 병렬 수집
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
