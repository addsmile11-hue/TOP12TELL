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
    """네이버 금융 시세 테이블 정밀 파싱 (숫자 추출 안정성 극대화)"""
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
            
            # 특수문자나 공백 제거 후 순수 숫자/소수점만 추출하여 에러 방지
            rate_text = tds[4].get_text(strip=True)
            rate_cleaned = re.sub(r'[^\d\.-]', '', rate_text)
            rate = float(rate_cleaned) if rate_cleaned else 0.0
            
            val_text = tds[6].get_text(strip=True)
            val_cleaned = re.sub(r'[^\d]', '', val_text)
            val = float(val_cleaned) if val_cleaned else 0.0
            
            stocks.append({'ticker': ticker, 'name': name, 'rate': rate, 'value': val})
        return stocks
    except:
        return []

def get_stock_data():
    """시총 및 거래대금 상위 50위 페이지 초고속 병렬 수집 (거래대금 URL 정밀 수정)"""
    urls = {
        "k_cap": "https://finance.naver.com/sise/sise_market_sum.naver?sosok=0&page=1",
        "kd_cap": "https://finance.naver.com/sise/sise_market_sum.naver?sosok=1&page=1",
        "k_val": "https://finance.naver.com/sise/sise_quant.naver?rankingType=deal_value&sosok=0",
        "kd_val": "https://finance.naver.com/sise/sise_quant.naver?rankingType=deal_value&sosok=1"
    }
    
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {key: executor.submit(parse_naver_sise, url) for key, url in urls.items()}
        results = {key: f.result() for key, f in futures.items()}
        
    # 시가총액 상위 50위 기준 정렬 및 상하위 3개 추출
    combined_cap = results["k_cap"] + results["kd_cap"]
    combined_cap.sort(key=lambda x: x['value'], reverse=True)
    top_50_cap = combined_cap[:50]
    top_50_cap.sort(key=lambda x: x['rate'], reverse=True)
    
    # 거래대금 상위 50위 기준 정렬 및 상하위 3개 추출
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
                
                # ⚡ 1차 고속 데이터 스크리닝 실행
                stock_data = get_stock_data()
                
                # 📊 요청하신 줄바꿈 형태의 깔끔한 문자열 포맷팅 생성
                cap_up_str = "\n".join([f"{s['name']} ({s['rate']}%)\n" for s in stock_data["market_cap"]["up"]]).strip()
                cap_down_str = "\n".join([f"{s['name']} ({s['rate']}%)\n" for s in stock_data["market_cap"]["down"]]).strip()
                val_up_str = "\n".join([f"{s['name']} ({s['rate']}%)\n" for s in stock_data["trading_volume"]["up"]]).strip()
                val_down_str = "\n".join([f"{s['name']} ({s['rate']}%)\n" for s in stock_data["trading_volume"]["down"]]).strip()
                
                # 🚀 [메시지 2] 12개 라인업 최종 브리핑 발송 (공란 버그 해결 및 가독성 최적화)
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
