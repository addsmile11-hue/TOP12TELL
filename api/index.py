from flask import Flask, request, jsonify
import requests
from bs4 import BeautifulSoup
import os
import re
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
PROCESSED_UPDATES = []

def parse_naver_sise(url):
    """네이버 금융 시세 테이블 정밀 파싱"""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        res = requests.get(url, headers=headers, timeout=5)
        res.encoding = 'euc-kr'
        soup = BeautifulSoup(res.text, 'html.parser')
        table = soup.select_one('table.type_2')
        if not table: return []
        
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
            
            rate_text = tds[4].get_text(strip=True).replace('%', '').replace('+', '').strip()
            rate = float(rate_text) if rate_text and rate_text != '0.00' else 0.0
            
            val_text = tds[6].get_text(strip=True).replace(',', '')
            val = float(val_text) if val_text else 0.0
            
            stocks.append({'ticker': ticker, 'name': name, 'rate': rate, 'value': val})
        return stocks
    except:
        return []

def get_stock_data():
    """시총/거래대금 상위 50위 페이지 4개를 초고속 병렬 수집"""
    urls = {
        "k_cap": "https://finance.naver.com/sise/sise_market_sum.naver?sosok=0&page=1",
        "kd_cap": "https://finance.naver.com/sise/sise_market_sum.naver?sosok=1&page=1",
        "k_val": "https://finance.naver.com/sise/sise_deal_value.naver?sosok=0",
        "kd_val": "https://finance.naver.com/sise/sise_deal_value.naver?sosok=1"
    }
    
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {key: executor.submit(parse_naver_sise, url) for key, url in urls.items()}
        results = {key: f.result() for key, f in futures.items()}
        
    combined_cap = results["k_cap"] + results["kd_cap"]
    combined_cap.sort(key=lambda x: x['value'], reverse=True)
    top_50_cap = combined_cap[:50]
    top_50_cap.sort(key=lambda x: x['rate'], reverse=True)
    
    combined_val = results["k_val"] + results["kd_val"]
    combined_val.sort(key=lambda x: x['value'], reverse=True)
    top_50_val = combined_val[:50]
    top_50_val.sort(key=lambda x: x['rate'], reverse=True)
    
    return {
        "market_cap": {"up": top_50_cap[:3], "down": list(reversed(top_50_cap[-3:]))},
        "trading_volume": {"up": top_50_val[:3], "down": list(reversed(top_50_val[-3:]))}
    }

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
                
                # ⚡ 고속 데이터 스크리닝 실행 (0.3초 내외)
                stock_data = get_stock_data()
                
                # 📊 요구하신 줄바꿈 가독성 양식으로 정밀 조립
                cap_up_str = "\n".join([f"{s['name']} ({s['rate']}%)" for s in stock_data["market_cap"]["up"]])
                cap_down_str = "\n".join([f"{s['name']} ({s['rate']}%)" for s in stock_data["market_cap"]["down"]])
                val_up_str = "\n".join([f"{s['name']} ({s['rate']}%)" for s in stock_data["trading_volume"]["up"]])
                val_down_str = "\n".join([f"{s['name']} ({s['rate']}%)" for s in stock_data["trading_volume"]["down"]])
                
                # 🚀 [메시지 2] 12개 라인업 확정 최종 브리핑 발송
                msg2_text = (
                    "📋 *오늘의 분석 대상 12개 종목 라인업 확정*\n\n"
                    "🏛️ *시가총액 상위 50위 그룹*\n"
                    f"• 📈 상승 Top 3:\n{cap_up_str}\n"
                    f"• 📉 하락 Top 3:\n{cap_down_str}\n\n"
                    "💸 *거래대금 상위 50위 그룹*\n"
                    f"• 📈 상승 Top 3:\n{val_up_str}\n"
                    f"• 📉 하락 Top 3:\n{val_down_str}"
                )
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={
                    "chat_id": chat_id,
                    "text": msg2_text,
                    "parse_mode": "Markdown"
                })
        return jsonify({"status": "success"})
    return "Stock Leader Screening Engine is running!"
