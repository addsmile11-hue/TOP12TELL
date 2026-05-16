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
    """네이버 금융 시세 메뉴 페이지 파싱 (EUC-KR 유지)"""
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

def get_stock_fundamentals(ticker):
    """개별 종목 상세 페이지 파싱 (근본 원인 해결: 네이버 상세페이지의 UTF-8 변환 대응)"""
    url = f"https://finance.naver.com/item/main.naver?code={ticker}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        res = requests.get(url, headers=headers, timeout=3)
        # 인코딩 설정을 생략하거나 utf-8로 두어 글자 깨짐(議 버그)을 완벽하게 방지합니다.
        res.encoding = 'utf-8' 
        soup = BeautifulSoup(res.text, 'html.parser')
        
        # 1. 시가총액 추출
        market_sum_elem = soup.select_one('#_market_sum')
        if market_sum_elem:
            market_sum = market_sum_elem.get_text(strip=True)
            market_sum = re.sub(r'\s+', ' ', market_sum)
            if not market_sum.endswith('억'):
                market_sum += '억'
        else:
            market_sum = "N/A"
            
        # 2. 추정 PER 또는 일반 PER 추출
        per_elem = soup.select_one('#_cper')  # 추정PER 조준
        if not per_elem or not per_elem.get_text(strip=True) or per_elem.get_text(strip=True).strip() == '-':
            per_elem = soup.select_one('#_per')   # 일반PER 대체 조준
            
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
        
    return stock_dict

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
                
                # ⚡ 고속 스크리닝 엔진 구동
                stock_data = get_stock_data()
                
                # 📊 요청하신 형태 그대로 줄바꿈 배치 포맷팅 생성 (대괄호 제거 및 가독성 확보)
                def make_formatted_lines(stocks):
                    lines = []
                    for s in stocks:
                        lines.append(f"{s['name']} ({s['rate']}%)\n시가총액 {s['market_sum']} 추정 PER {s['per']}\n")
                    return "\n".join(lines).strip()
                
                cap_up_str = make_formatted_lines(stock_data["market_cap"]["up"])
                cap_down_str = make_formatted_lines(stock_data["market_cap"]["down"])
                val_up_str = make_formatted_lines(stock_data["trading_volume"]["up"])
                val_down_str = make_formatted_lines(stock_data["trading_volume"]["down"])
                
                # 🚀 [메시지 2] 완벽하게 가공된 12개 최종 라인업 리포트 전송
                msg2_text = (
                    "📋 *오늘의 분석 대상 12개 종목 라인업 확정*\n\n"
                    "🏛️ *시가총액 상위 50위 그룹*\n\n"
                    f"• 📈 상승 Top 3:\n{cap_up_str}\n\n"
                    f"• 📉 하락 Top 3:\n{cap_down_str}\n\n"
                    "💸 *거래대금 상위 50위 그룹*\n\n"
                    f"• 📈 상승 Top 3:\n{val_up_str}\n\n"
                    f"• 📉 하락 Top 3:\n{val_down_str}"
                )
                
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={
                    "chat_id": chat_id,
                    "text": msg2_text,
                    "parse_mode": "Markdown"
                })
        return jsonify({"status": "success"})
    return "Stock Leader Screening Engine is running!"
