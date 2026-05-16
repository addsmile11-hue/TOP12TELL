from flask import Flask, request, jsonify
import requests
from bs4 import BeautifulSoup
import google.generativeai as genai
import os
import re
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)

# 중복 요청 방지용 필터 고리
PROCESSED_UPDATES = []

def parse_naver_sise(url):
    """네이버 금융 시세 테이블 범용 파싱 (클래스 필터 제거로 버그 원천 차단)"""
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
            
            # [버셀 버그 수정] .tltle 클래스 제약을 없애고 td 안의 첫 번째 a 태그를 바로 조준합니다.
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
        "k_val": "https://finance.naver.com/sise/sise_value_deal.naver?sosok=0",
        "kd_val": "https://finance.naver.com/sise/sise_value_deal.naver?sosok=1"
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

def get_naver_finance_news(stock_info):
    """종목별 최신 뉴스 3개 수집"""
    ticker = stock_info['ticker']
    url = f"https://finance.naver.com/item/news_news.naver?code={ticker}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        res = requests.get(url, headers=headers, timeout=3)
        res.encoding = 'euc-kr'
        soup = BeautifulSoup(res.text, 'html.parser')
        rows = soup.select('table.type5 > tbody > tr')
        collected = []
        for row in rows:
            if 'relation_lst' in str(row.get('class', '')): continue
            title_elem = row.select_one('td.title > a.tit')
            info_elem = row.select_one('td.info')
            if title_elem:
                collected.append(f"▶ [{info_elem.get_text(strip=True) if info_elem else '미상'}] {title_elem.get_text(strip=True)}")
            if len(collected) >= 3: break
        return stock_info, "\n".join(collected) if collected else "최근 관련 뉴스 없음"
    except:
        return stock_info, "뉴스 크롤링 오류"

def run_main_pipeline(data):
    """수집된 데이터에 뉴스를 매핑하고 제미나이 AI 분석 요청"""
    tasks = []
    for item in data["market_cap"]["up"]: tasks.append((item, "up", "시가총액 상위 50위"))
    for item in data["market_cap"]["down"]: tasks.append((item, "down", "시가총액 상위 50위"))
    for item in data["trading_volume"]["up"]: tasks.append((item, "up", "거래대금 상위 50위"))
    for item in data["trading_volume"]["down"]: tasks.append((item, "down", "거래대금 상위 50위"))
    
    def fetch_wrapper(t):
        s_info, direction, group = t
        _, news_text = get_naver_finance_news(s_info)
        return s_info, news_text, direction, group
        
    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(fetch_wrapper, tasks))
        
    input_data_for_llm = ""
    current_group, current_dir = "", ""
    for s_info, news_text, direction, group in results:
        if current_group != group:
            current_group = group
            input_data_for_llm += f"\n## 🏆 [{current_group}] 등락률 Top 3\n"
        dir_title = "📈 상승 Top 3" if direction == "up" else "📉 하락 Top 3"
        if current_dir != dir_title:
            current_dir = dir_title
            input_data_for_llm += f"### {current_dir}\n"
        input_data_for_llm += f"- {s_info['name']} ({s_info['rate']}%)\n{news_text}\n"

    system_instruction = """
    당신은 대한민국 주식 시장의 트렌드 변화와 주도 섹터를 포착하는 전문 기관 투자자(프로 트레이더)이자 금융 분석가입니다.
    [Analysis Criteria]
    1. 상승 종목 분석 시: 단순 테마성 순환매인지, 실적 어닝 서프라이즈, 핵심 수주 등 연속성이 있는 '새로운 논리'인지 명확히 구분하세요.
    2. 하락 종목 분석 시: 고점 차익실현인지, 펀더멘털 훼손 같은 '구조적 악재'인지 판단하세요.
    3. 미사여구는 절대 생략하고 팩트 위주로 극도로 압축하세요.
    [Output Format]을 엄격히 준수하여 12개 종목 모두 출력하세요. 종목당 핵심 요약은 최대 2줄 제한입니다.
    """
    try:
        model = genai.GenerativeModel(model_name="gemini-1.5-flash", system_instruction=system_instruction)
        response = model.generate_content(f"[Input Data]\n{input_data_for_llm}", generation_config={"temperature": 0.2})
        return response.text
    except Exception as e:
        return f"⚠️ 제미나이 AI 분석 처리 중 오류가 발생했습니다: {str(e)}"

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
                
                # 🚀 [메시지 2] 발굴된 12개 분석 대상 종목 라인업 가독성 업그레이드 버전 발송
                msg2_text = (
                    "📋 *오늘의 분석 대상 12개 종목 라인업 확정*\n\n"
                    "🏛️ *시가총액 상위 50위 그룹*\n"
                    f"• 📈 상승 Top 3: " + ", ".join([f"*{s['name']}* ({s['rate']}%)" for s in stock_data["market_cap"]["up"]]) + "\n"
                    f"• 📉 하락 Top 3: " + ", ".join([f"*{s['name']}* ({s['rate']}%)" for s in stock_data["market_cap"]["down"]]) + "\n\n"
                    "💸 *거래대금 상위 50위 그룹*\n"
                    f"• 📈 상승 Top 3: " + ", ".join([f"*{s['name']}* ({s['rate']}%)" for s in stock_data["trading_volume"]["up"]]) + "\n"
                    f"• 📉 하락 Top 3: " + ", ".join([f"*{s['name']}* ({s['rate']}%)" for s in stock_data["trading_volume"]["down"]]) + "\n\n"
                    "⏳ 제미나이 AI가 위 종목들의 뉴스 플로우와 주도주 내러티브를 정밀 분석하고 있습니다..."
                )
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={
                    "chat_id": chat_id,
                    "text": msg2_text,
                    "parse_mode": "Markdown"
                })
                
                # ⚡ 2차 뉴스 긁기 및 제미나이 연산 처리
                analysis_result = run_main_pipeline(stock_data)
                
                # 🚀 [메시지 3] 최종 주도주 모멘텀 분석 리포트 발송
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={
                    "chat_id": chat_id,
                    "text": analysis_result,
                    "parse_mode": "Markdown"
                })
        return jsonify({"status": "success"})
    return "Stock Leader Analysis Bot Engine is running!"
