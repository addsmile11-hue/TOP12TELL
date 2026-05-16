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

def parse_naver_sise(url):
    """네이버 금융 시세 메뉴 페이지 파싱 및 실제 거래일 추출"""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        res = requests.get(url, headers=headers, timeout=5)
        res.encoding = 'euc-kr'
        
        # 📅 [근본 수정] 네이버 금융 페이지 내에 표기된 실제 마켓 거래일(YYYY.MM.DD) 조준 추출
        date_match = re.search(r'(\d{4})\.(\d{2})\.(\d{2})', res.text)
        market_date = f"{date_match.group(1)}년 {date_match.group(2)}월 {date_match.group(3)}일" if date_match else None
        
        soup = BeautifulSoup(res.text, 'html.parser')
        table = soup.select_one('table.type_2')
        if not table: return [], market_date
        
        rows = table.select('tr')
        stocks = []
        for row in rows:
            tds = row.select('td')
            if len(tds) < 7: continue
            
            a_tag = tds[1].select_one('a')
            if not a_tag or 'href' not in a_tag.attrs: continue
            
            name = a_tag.get_text(strip=True)
            ticker_match = re.search(r'code=(\d+)', a_tag.get('href', ''))
            if not ticker_match: continue
            ticker = ticker_match.group(1)
            
            rate_text = tds[4].get_text(strip=True)
            rate_cleaned = re.sub(r'[^\d\.-]', '', rate_text)
            rate = float(rate_cleaned) if rate_cleaned else 0.0
            
            val_text = tds[6].get_text(strip=True)
            val_cleaned = re.sub(r'[^\d]', '', val_text)
            val = float(val_cleaned) if val_cleaned else 0.0
            
            stocks.append({'ticker': ticker, 'name': name, 'rate': rate, 'value': val})
        return stocks, market_date
    except:
        return [], None

def get_stock_fundamentals(ticker):
    """개별 종목 상세 페이지 파싱 (시가총액 띄어쓰기 결합 완료)"""
    url = f"https://finance.naver.com/item/main.naver?code={ticker}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        res = requests.get(url, headers=headers, timeout=3)
        res.encoding = 'utf-8' 
        soup = BeautifulSoup(res.text, 'html.parser')
        
        market_sum_elem = soup.select_one('#_market_sum')
        if market_sum_elem:
            market_sum = market_sum_elem.get_text(strip=True)
            market_sum = re.sub(r'\s+', ' ', market_sum)
            market_sum = market_sum.replace('조 ', '조')
            if not market_sum.endswith('억'):
                market_sum += '억'
        else:
            market_sum = "N/A"
            
        per_elem = soup.select_one('#_cper')
        if not per_elem or not per_elem.get_text(strip=True) or per_elem.get_text(strip=True).strip() == '-':
            per_elem = soup.select_one('#_per')
            
        if per_elem:
            per = per_elem.get_text(strip=True).strip()
            per = "N/A" if per == '-' or not per else per
        else:
            per = "N/A"
            
        return market_sum, per
    except:
        return "N/A", "N/A"

def get_stock_data():
    """시총/거래대금 데이터 병렬 수집 및 종목 매핑"""
    urls = {
        "k_cap": "https://finance.naver.com/sise/sise_market_sum.naver?sosok=0&page=1",
        "kd_cap": "https://finance.naver.com/sise/sise_market_sum.naver?sosok=1&page=1",
        "k_val": "https://finance.naver.com/sise/sise_quant.naver?rankingType=deal_value&sosok=0",
        "kd_val": "https://finance.naver.com/sise/sise_quant.naver?rankingType=deal_value&sosok=1"
    }
    
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {key: executor.submit(parse_naver_sise, url) for key, url in urls.items()}
        results = {key: f.result() for key, f in futures.items()}
        
    k_cap_stocks, market_date = results["k_cap"]
    kd_cap_stocks, _ = results["kd_cap"]
    k_val_stocks, _ = results["k_val"]
    kd_val_stocks, _ = results["kd_val"]
    
    # 데이터 수집 페이지 중 하나라도 날짜가 확보되었다면 해당 날짜를 기준일로 설정
    if not market_date:
        for res in results.values():
            if res[1]:
                market_date = res[1]
                break
    if not market_date:
        market_date = datetime.datetime.now().strftime("%Y년 %m월 %d일")
        
    combined_cap = k_cap_stocks + kd_cap_stocks
    combined_cap.sort(key=lambda x: x['value'], reverse=True)
    top_50_cap = combined_cap[:50]
    top_50_cap.sort(key=lambda x: x['rate'], reverse=True)
    
    combined_val = k_val_stocks + kd_val_stocks
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
        
    return stock_dict, market_date

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
                # 🚀 [메시지 1] 즉시 대기 안내 텍스트 발송
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={
                    "chat_id": chat_id, 
                    "text": "🔄 당일 거래대금/시총 상위 종목을 스크리닝 중입니다. 잠시만 기다려주세요..."
                })
                
                # ⚡ 고속 스크리닝 엔진 구동 (실제 데이터 기준 날짜를 함께 받아옴)
                stock_data, date_str = get_stock_data()
                
                # 📊 줄바꿈 및 🔹 이모티콘 포맷터
                def make_formatted_lines(stocks):
                    lines = []
                    for s in stocks:
                        lines.append(f"🔹 *{s['name']}* ({s['rate']}%)\n시가총액 {s['market_sum']} 추정 PER {s['per']}")
                    return "\n".join(lines)
                
                cap_up_str = make_formatted_lines(stock_data["market_cap"]["up"])
                cap_down_str = make_formatted_lines(stock_data["market_cap"]["down"])
                val_up_str = make_formatted_lines(stock_data["trading_volume"]["up"])
                val_down_str = make_formatted_lines(stock_data["trading_volume"]["down"])
                
                # ✍️ 다른 LLM 검색용 프롬프트 질문 상단 템플릿 정의
                prompt_template = (
                    "아래 12개의 종목에서 \n"
                    "오늘 실적발표가 있었던 종목이 있는지 알려줘. \n"
                    "오늘 \"최초\"와 관련된 종목이 있는지 알려줘. \n"
                    "오늘 \"공시\"와 관련된 종목이 있는지 알려줘.\n\n\n\n"
                )
                
                # 🚀 [메시지 2] 네이버 실시간 역추적 날짜가 연동된 최종 리포트 결합 발송
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
