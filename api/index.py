from flask import Flask, request, jsonify
import requests
from bs4 import BeautifulSoup
import os
import re
import datetime
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
PROCESSED_UPDATES = []


# ============================================================
# 📅 영업일(거래일) 계산 모듈 - 새로 추가된 부분
# ============================================================

# 2026년 한국 증시 휴장일 (필요시 매년 갱신)
KRX_HOLIDAYS_2026 = {
    datetime.date(2026, 1, 1),   # 신정
    datetime.date(2026, 2, 16),  # 설 대체
    datetime.date(2026, 2, 17),  # 설
    datetime.date(2026, 2, 18),  # 설
    datetime.date(2026, 3, 1),   # 삼일절 (일)
    datetime.date(2026, 3, 2),   # 삼일절 대체
    datetime.date(2026, 5, 5),   # 어린이날
    datetime.date(2026, 5, 25),  # 부처님오신날
    datetime.date(2026, 6, 3),   # 지방선거
    datetime.date(2026, 6, 6),   # 현충일 (토)
    datetime.date(2026, 8, 15),  # 광복절 (토)
    datetime.date(2026, 9, 24),  # 추석
    datetime.date(2026, 9, 25),  # 추석
    datetime.date(2026, 9, 26),  # 추석 (토)
    datetime.date(2026, 10, 3),  # 개천절 (토)
    datetime.date(2026, 10, 9),  # 한글날
    datetime.date(2026, 12, 25), # 크리스마스
    datetime.date(2026, 12, 31), # 연말 휴장
}

KRX_HOLIDAYS_2025 = {
    datetime.date(2025, 1, 1),
    datetime.date(2025, 1, 28),
    datetime.date(2025, 1, 29),
    datetime.date(2025, 1, 30),
    datetime.date(2025, 3, 3),
    datetime.date(2025, 5, 5),
    datetime.date(2025, 5, 6),
    datetime.date(2025, 6, 6),
    datetime.date(2025, 8, 15),
    datetime.date(2025, 10, 3),
    datetime.date(2025, 10, 6),
    datetime.date(2025, 10, 7),
    datetime.date(2025, 10, 8),
    datetime.date(2025, 10, 9),
    datetime.date(2025, 12, 25),
    datetime.date(2025, 12, 31),
}

ALL_KRX_HOLIDAYS = KRX_HOLIDAYS_2025 | KRX_HOLIDAYS_2026


def get_last_trading_day(now=None):
    """
    현재 시각 기준 '시세 페이지가 보여주는 데이터의 거래일'을 계산.
    - 평일 장 마감(15:30) 이전이면 직전 영업일
    - 평일 장 마감 이후면 당일
    - 주말/공휴일이면 직전 영업일
    """
    if now is None:
        now = datetime.datetime.now()
    
    today = now.date()
    # 평일이고 15:30 이전이면 아직 당일 데이터가 없음 → 직전 영업일
    if today.weekday() < 5 and today not in ALL_KRX_HOLIDAYS:
        if now.hour < 15 or (now.hour == 15 and now.minute < 30):
            candidate = today - datetime.timedelta(days=1)
        else:
            candidate = today
    else:
        candidate = today
    
    # 주말/공휴일이면 영업일을 만날 때까지 하루씩 되돌림
    while candidate.weekday() >= 5 or candidate in ALL_KRX_HOLIDAYS:
        candidate -= datetime.timedelta(days=1)
    
    return candidate


def format_trading_day(d):
    days = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]
    return f"{d.year}년 {d.month:02d}월 {d.day:02d}일 {days[d.weekday()]}"


# ============================================================
# 시세 파싱 - 날짜 추출 로직 제거 (영업일 계산으로 대체)
# ============================================================

def parse_naver_sise(url):
    """네이버 금융 시세 페이지 파싱 (날짜 추출은 더 이상 여기서 하지 않음)"""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        res = requests.get(url, headers=headers, timeout=5)
        res.encoding = 'euc-kr'

        soup = BeautifulSoup(res.text, 'html.parser')
        table = soup.select_one('table.type_2')
        if not table:
            return []

        rows = table.select('tr')
        stocks = []
        for row in rows:
            tds = row.select('td')
            if len(tds) < 7:
                continue

            a_tag = tds[1].select_one('a')
            if not a_tag or 'href' not in a_tag.attrs:
                continue

            name = a_tag.get_text(strip=True)
            ticker_match = re.search(r'code=(\d+)', a_tag.get('href', ''))
            if not ticker_match:
                continue
            ticker = ticker_match.group(1)

            rate_text = tds[4].get_text(strip=True)
            rate_cleaned = re.sub(r'[^\d\.-]', '', rate_text)
            rate = float(rate_cleaned) if rate_cleaned else 0.0

            val_text = tds[6].get_text(strip=True)
            val_cleaned = re.sub(r'[^\d]', '', val_text)
            val = float(val_cleaned) if val_cleaned else 0.0

            stocks.append({'ticker': ticker, 'name': name, 'rate': rate, 'value': val})
        return stocks
    except Exception:
        return []


def get_stock_fundamentals(ticker):
    """개별 종목 상세 지표 파싱"""
    url = f"https://finance.naver.com/item/main.naver?code={ticker}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        res = requests.get(url, headers=headers, timeout=3)
        res.encoding = 'utf-8'
        soup = BeautifulSoup(res.text, 'html.parser')

        market_sum_elem = soup.select_one('#_market_sum')
        if market_sum_elem:
            market_sum = market_sum_elem.get_text(strip=True)
            market_sum = re.sub(r'\s+', ' ', market_sum).replace('조 ', '조')
            if not market_sum.endswith('억'):
                market_sum += '억'
        else:
            market_sum = "N/A"

        per_elem = soup.select_one('#_cper')
        if not per_elem or not per_elem.get_text(strip=True) or per_elem.get_text(strip=True).strip() == '-':
            per_elem = soup.select_one('#_per')

        per = per_elem.get_text(strip=True).strip() if per_elem else "N/A"
        if per == '-':
            per = "N/A"

        return market_sum, per
    except Exception:
        return "N/A", "N/A"


def get_stock_data():
    """4개 페이지 데이터 병렬 수집 및 최종 통합 처리"""
    urls = {
        "k_cap": "https://finance.naver.com/sise/sise_market_sum.naver?sosok=0&page=1",
        "kd_cap": "https://finance.naver.com/sise/sise_market_sum.naver?sosok=1&page=1",
        "k_val": "https://finance.naver.com/sise/sise_quant.naver?rankingType=deal_value&sosok=0",
        "kd_val": "https://finance.naver.com/sise/sise_quant.naver?rankingType=deal_value&sosok=1"
    }

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {key: executor.submit(parse_naver_sise, url) for key, url in urls.items()}
        results = {key: f.result() for key, f in futures.items()}

    # ✅ 날짜는 영업일 계산기로 결정 (페이지 텍스트에 의존 X)
    final_date = format_trading_day(get_last_trading_day())

    combined_cap = results["k_cap"] + results["kd_cap"]
    combined_cap.sort(key=lambda x: x['value'], reverse=True)
    top_50_cap = combined_cap[:50]
    top_50_cap.sort(key=lambda x: x['rate'], reverse=True)

    combined_val = results["k_val"] + results["kd_val"]
    combined_val.sort(key=lambda x: x['value'], reverse=True)
    top_50_val = combined_val[:50]
    top_50_val.sort(key=lambda x: x['rate'], reverse=True)

    stock_dict = {
        "market_cap": {"up": top_50_cap[:3], "down": list(reversed(top_50_cap[-3:]))},
        "trading_volume": {"up": top_50_val[:3], "down": list(reversed(top_50_val[-3:]))}
    }

    flat_stocks = []
    for group in ["market_cap", "trading_volume"]:
        for direction in ["up", "down"]:
            flat_stocks.extend(stock_dict[group][direction])

    with ThreadPoolExecutor(max_workers=12) as executor:
        fundamental_results = list(executor.map(lambda s: get_stock_fundamentals(s['ticker']), flat_stocks))

    for s, (m_sum, per) in zip(flat_stocks, fundamental_results):
        s['market_sum'] = m_sum
        s['per'] = per

    return stock_dict, final_date


@app.route('/', methods=['POST', 'GET'])
def telegram_webhook():
    global PROCESSED_UPDATES
    if request.method == 'POST':
        update = request.get_json()

        update_id = update.get("update_id")
        if update_id:
            if update_id in PROCESSED_UPDATES:
                return jsonify({"status": "ignored_duplicate"})
            PROCESSED_UPDATES.append(update_id)
            if len(PROCESSED_UPDATES) > 50:
                PROCESSED_UPDATES.pop(0)

        if "message" in update and "text" in update["message"]:
            text = update["message"]["text"]
            chat_id = update["message"]["chat"]["id"]

            if text == '/check':
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={
                    "chat_id": chat_id,
                    "text": "🔄 당일 거래대금/시총 상위 종목을 스크리닝 중입니다. 잠시만 기다려주세요..."
                })

                stock_data, date_str = get_stock_data()

                def make_formatted_lines(stocks):
                    lines = []
                    for s in stocks:
                        lines.append(f"🔹 *{s['name']}* ({s['rate']}%)\n시가총액 {s['market_sum']} 추정 PER {s['per']}")
                    return "\n".join(lines)

                cap_up_str = make_formatted_lines(stock_data["market_cap"]["up"])
                cap_down_str = make_formatted_lines(stock_data["market_cap"]["down"])
                val_up_str = make_formatted_lines(stock_data["trading_volume"]["up"])
                val_down_str = make_formatted_lines(stock_data["trading_volume"]["down"])

                prompt_template = (
                    "아래 12개의 종목에서 \n"
                    "오늘 실적발표가 있었던 종목이 있는지 알려줘. \n"
                    "오늘 \"최초\"와 관련된 종목이 있는지 알려줘. \n"
                    "오늘 \"공시\"와 관련된 종목이 있는지 알려줘.\n\n\n\n"
                )

                msg2_text = (
                    f"{prompt_template}"
                    f"📋 *[{date_str}] 분석 대상 12개 종목 라인업 확정*\n\n"
                    "🏛️ *시가총액 상위 50위 그룹*\n\n"
                    f"• 📈 상승 Top 3\n\n{cap_up_str}\n\n"
                    f"• 📉 하락 Top 3\n\n{cap_down_str}\n\n"
                    "💸 *거래대금 상위 50위 그룹*\n\n"
                    f"• 📈 상승 Top 3\n\n{val_up_str}\n\n"
                    f"• 📉 하락 Top 3\n\n{val_down_str}"
                )

                requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={
                    "chat_id": chat_id,
                    "text": msg2_text,
                    "parse_mode": "Markdown"
                })
        return jsonify({"status": "success"})
    return "Stock Leader Screening Engine is running!"
