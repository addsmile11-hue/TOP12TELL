from flask import Flask, request, jsonify
import requests
from bs4 import BeautifulSoup
import google.generativeai as genai
import datetime
import os
from concurrent.futures import ThreadPoolExecutor
from pykrx import stock

app = Flask(__name__)

# 환경변수에서 토큰 및 API 키 로드 (버셀 대시보드에서 등록할 예정)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

genai.configure(api_key=GEMINI_API_KEY)

def get_naver_finance_news(item):
    """개별 종목 뉴스 크롤링 (멀티스레딩용)"""
    ticker, name, pct, direction, group = item
    url = f"https://finance.naver.com/item/news_news.naver?code={ticker}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    
    try:
        res = requests.get(url, headers=headers, timeout=3)
        res.encoding = 'euc-kr'
        if res.status_code != 200: return item, "뉴스 수집 실패"
        
        soup = BeautifulSoup(res.text, 'html.parser')
        rows = soup.select('table.type5 > tbody > tr')
        collected = []
        for row in rows:
            if 'relation_lst' in str(row.get('class', '')): continue
            title_elem = row.select_one('td.title > a.tit')
            info_elem = row.select_one('td.info')
            if title_elem:
                title = title_elem.get_text(strip=True)
                info = info_elem.get_text(strip=True) if info_elem else "미상"
                collected.append(f"▶ [{info}] {title}")
            if len(collected) >= 3: break
        return item, "\n".join(collected) if collected else "최근 관련 뉴스 없음"
    except:
        return item, "크롤링 중 오류 발생"

def run_main_pipeline():
    """데이터 스크리닝 및 제미나이 분석 총괄"""
    # 1. 영업일 기준 당일 날짜 확보
    target_date = datetime.datetime.now().strftime("%Y%m%d")
    
    # 2. pykrx 데이터 로드
    df_cap_data = stock.get_market_cap_by_ticker(target_date, market="ALL")
    df_ohlcv_data = stock.get_market_ohlcv_by_ticker(target_date, market="ALL")
    
    # 3. 데이터 추출 및 작업 큐(Queue) 생성
    tasks = []
    
    # 시가총액 상위 50위 그룹
    c_top50 = df_cap_data.nlargest(50, '시가총액').index
    c_pool = df_ohlcv_data.loc[c_top50, ['등락률']]
    for t in c_pool.nlargest(3, '등락률').index: tasks.append((t, stock.get_market_ticker_name(t), round(c_pool.loc[t, '등락률'], 2), "up", "시가총액 상위 50위"))
    for t in c_pool.nsmallest(3, '등락률').index: tasks.append((t, stock.get_market_ticker_name(t), round(c_pool.loc[t, '등락률'], 2), "down", "시가총액 상위 50위"))
    
    # 거래대금 상위 50위 그룹
    v_top50 = df_cap_data.nlargest(50, '거래대금').index
    v_pool = df_ohlcv_data.loc[v_top50, ['등락률']]
    for t in v_pool.nlargest(3, '등락률').index: tasks.append((t, stock.get_market_ticker_name(t), round(v_pool.loc[t, '등락률'], 2), "up", "거래대금 상위 50위"))
    for t in v_pool.nsmallest(3, '등락률').index: tasks.append((t, stock.get_market_ticker_name(t), round(v_pool.loc[t, '등락률'], 2), "down", "거래대금 상위 50위"))

    # 4. 멀티스레딩으로 12개 종목 뉴스 초고속 병렬 수집 (Timeout 방지 핵심)
    input_data_for_llm = ""
    current_group = ""
    current_dir = ""
    
    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(get_naver_finance_news, tasks))
    
    for item, news_text in results:
        ticker, name, pct, direction, group = item
        if current_group != group:
            current_group = group
            input_data_for_llm += f"\n## 🏆 [{current_group}] 등락률 Top 3\n"
        
        dir_title = "📈 상승 Top 3" if direction == "up" else "📉 하락 Top 3"
        if current_dir != dir_title:
            current_dir = dir_title
            input_data_for_llm += f"### {current_dir}\n"
            
        input_data_for_llm += f"- {name} ({pct}%)\n{news_text}\n"

    # 5. 제미나이 분석 요청
    system_instruction = """
    당신은 대한민국 주식 시장의 트렌드 변화와 주도 섹터를 포착하는 전문 기관 투자자(프로 트레이더)이자 금융 분석가입니다.
    [Analysis Criteria]
    1. 상승 종목 분석 시: 단순 테마성 순환매인지, 실적 어닝 서프라이즈, 핵심 수주 등 연속성이 있는 '새로운 논리'인지 명확히 구분하세요.
    2. 하락 종목 분석 시: 단순 차익실현인지, 펀더멘털 훼손 같은 '구조적 악재'인지 판단하세요.
    3. 미사여구는 절대 생략하고 팩트 위주로 극도로 압축하세요.
    [Output Format]을 엄격히 준수하여 12개 종목 모두 출력하세요. 종목당 핵심 요약은 최대 2줄 제한입니다.
    """
    
    model = genai.GenerativeModel(model_name="gemini-1.5-flash", system_instruction=system_instruction)
    response = model.generate_content(f"데이터:\n{input_data_for_llm}", generation_config={"temperature": 0.2})
    return response.text

@app.route('/', methods=['POST', 'GET'])
def telegram_webhook():
    if request.method == 'POST':
        update = request.get_json()
        if "message" in update and "text" in update["message"]:
            text = update["message"]["text"]
            chat_id = update["message"]["chat"]["id"]
            
            if text == '/check':
                # 우선 "분석중" 메시지 알림 날리기
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": "🔄 당일 주도주 라인업 스크리닝 및 뉴스 플로우 분석 중입니다. 잠시만 기다려주세요..."})
                
                # 메인 분석 실행
                analysis_result = run_main_pipeline()
                
                # 최종 결과 발송 (마크다운 포맷 적용)
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={
                    "chat_id": chat_id,
                    "text": analysis_result,
                    "parse_mode": "Markdown"
                })
        return jsonify({"status": "success"})
    return "Bot Server is Running"
