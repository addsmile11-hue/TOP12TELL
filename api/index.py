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

# 텔레그램의 성급한 중복 재요청(Retry)을 기억하고 차단하기 위한 글로벌 저장소
PROCESSED_UPDATES = []

def parse_naver_sise(url):
    """네이버 금융 시세 테이블을 정밀 타겟팅하여 종목 데이터를 파싱합니다."""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        res = requests.get(url, headers=headers, timeout=5)
        res.encoding = 'euc-kr'
        soup = BeautifulSoup(res.text, 'html.parser')
        table = soup.select_one('table.type_2')
        if not table: return []
        
        rows = table.select('tbody > tr')
        stocks = []
        for row in rows:
            tds = row.select('td')
            if len(tds) < 7: continue
            
            a_tag = tds[1].select_one('a.tltle')
            if not a_tag: continue
            
            name = a_tag.get_text(strip=True)
            ticker = re.search(r'code=(\d+)', a_tag.get('href', '')).group(1)
            
            rate_text = tds[4].get_text(strip=True).replace('%', '').replace('+', '').strip()
            rate = float(rate_text) if rate_text and rate_text != '0.00' else 0.0
            
            val_text = tds[6].get_text(strip=True).replace(',', '')
            val = float(val_text) if val_text else 0.0
            
            stocks.append({'ticker': ticker, 'name': name, 'rate': rate, 'value': val})
        return stocks
    except:
        return []

def get_stock_data():
    """4개의 네이버 시세 페이지를 동시에(병렬) 긁어와 시간을 1/4로 단축합니다."""
    urls = {
        "k_cap": "https://finance.naver.com/sise/sise_market_sum.naver?sosok=0&page=1",
        "kd_cap": "https://finance.naver.com/sise/sise_market_sum.naver?sosok=1&page=1",
        "k_val": "https://finance.naver.com/sise/sise_value_deal.naver?sosok=0",
        "kd_val": "https://finance.naver.com/sise/sise_value_deal.naver?sosok=1"
    }
    
    # 4개의 주소를 4명의 일꾼에게 각각 맡겨 동시에 처리
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {key: executor.submit(parse_naver_sise, url) for key, url in urls.items()}
        results = {key: f.result() for key, f in futures.items()}
        
    # 1. 시가총액 상위 50위 통합 및 정렬
    combined_cap = results["k_cap"] + results["kd_cap"]
    combined_cap.sort(key=lambda x: x['value'], reverse=True)
    top_50_cap = combined_cap[:50]
    top_50_cap.sort(key=lambda x: x['rate'], reverse=True)
    
    # 2. 거래대금 상위 50위 통합 및 정렬
    combined_val = results["k_val"] + results["kd_val"]
    combined_val.sort(key=lambda x: x['value'], reverse=True)
    top_50_val = combined_val[:50]
    top_50_val.sort(key=lambda x: x['rate'], reverse=True)
    
    return {
        "market_cap": {"up": top_50_cap[:3], "down": list(reversed(top_50_cap[-3:]))},
        "trading_volume": {"up": top_50_val[:3], "down": list(reversed(top_50_val[-3:]))}
    }

def get_naver_finance_news(stock_info):
    """종목별 최신 뉴스 플로우를 수집합니다."""
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

def run_main_pipeline():
    """전체 스크리닝 및 제미나이 브리핑 생성 프로세스"""
    data = get_stock_data()
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
        
        # 중복 요청 강제 차단 필터 고리
        update_id = update.get("update_id")
        if update_id:
            if update_id in PROCESSED_UPDATES:
                return jsonify({"status": "ignored_duplicate"})  # 이미 처리 중인 중복 메시지면 즉시 종료
            PROCESSED_UPDATES.append(update_id)
            if len(PROCESSED_UPDATES) > 50:  # 메모리 관리를 위해 최근 50개만 유지
                PROCESSED_UPDATES.pop(0)

        if "message" in update and "text" in update["message"]:
            text = update["message"]["text"]
            chat_id = update["message"]["chat"]["id"]
            
            if text == '/check':
                # 1. 즉시 첫 안내 멘트 발송 (사용자 대기감 해소)
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={
                    "chat_id": chat_id, 
                    "text": "🔄 당일 거래대금/시총 상위 종목을 스크리닝하고 최신 뉴스 플로우를 분석 중입니다. 잠시만 기다려주세요..."
                })
                
                # 2. 극도로 빨라진 병렬 분석 파이프라인 가동
                analysis_result = run_main_pipeline()
                
                # 3. 리포트 결과 최종 발송
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={
                    "chat_id": chat_id,
                    "text": analysis_result,
                    "parse_mode": "Markdown"
                })
        return jsonify({"status": "success"})
    return "Stock Leader Analysis Bot Engine is running!"
