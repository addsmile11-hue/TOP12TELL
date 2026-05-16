from flask import Flask, request, jsonify
import requests
from bs4 import BeautifulSoup
import google.generativeai as genai
import datetime
import os
import pandas as pd
from concurrent.futures import ThreadPoolExecutor
from pykrx import stock

app = Flask(__name__)

# 버셀 환경변수에서 보안을 위해 숨겨둔 토큰과 API 키를 읽어옵니다.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

genai.configure(api_key=GEMINI_API_KEY)

def get_naver_finance_news(item):
    """네이버 금융에서 개별 종목의 최신 뉴스 3개를 긁어옵니다."""
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
    """데이터 스크리닝 및 제미나이 분석 총괄 파이프라인"""
    
    # [안전장치] 주말이나 공휴일이면 데이터가 있는 가장 최근 평일(영업일)을 찾을 때까지 일주일 뒤로 돌립니다.
    target_date = datetime.datetime.now()
    df_cap_data = pd.DataFrame()
    df_ohlcv_data = pd.DataFrame()
    
    for _ in range(7):
        date_str = target_date.strftime("%Y%m%d")
        try:
            df_cap_data = stock.get_market_cap_by_ticker(date_str, market="ALL")
            df_ohlcv_data = stock.get_market_ohlcv_by_ticker(date_str, market="ALL")
            if not df_cap_data.empty and not df_ohlcv_data.empty:
                break
        except:
            pass
        target_date -= datetime.timedelta(days=1)
        
    if df_cap_data.empty:
        return "⚠️ 최근 시장 데이터를 불러오지 못했습니다. 한국거래소(KRX) 서버 점검 중일 수 있습니다."

    tasks = []
    
    # 1. 시가총액 상위 50위 그룹 분석 대상 추출
    c_top50 = df_cap_data.nlargest(50, '시가총액').index
    c_pool = df_ohlcv_data.loc[c_top50, ['등락률']]
    for t in c_pool.nlargest(3, '등락률').index: tasks.append((t, stock.get_market_ticker_name(t), round(c_pool.loc[t, '등락률'], 2), "up", "시가총액 상위 50위"))
    for t in c_pool.nsmallest(3, '등락률').index: tasks.append((t, stock.get_market_ticker_name(t), round(c_pool.loc[t, '등락률'], 2), "down", "시가총액 상위 50위"))
    
    # 2. 거래대금 상위 50위 그룹 분석 대상 추출
    v_top50 = df_cap_data.nlargest(50, '거래대금').index
    v_pool = df_ohlcv_data.loc[v_top50, ['등락률']]
    for t in v_pool.nlargest(3, '등락률').index: tasks.append((t, stock.get_market_ticker_name(t), round(v_pool.loc[t, '등락률'], 2), "up", "거래대금 상위 50위"))
    for t in v_pool.nsmallest(3, '등락률').index: tasks.append((t, stock.get_market_ticker_name(t), round(v_pool.loc[t, '등락률'], 2), "down", "거래대금 상위 50위"))

    # 3. 12개 종목의 뉴스를 한 번에 초고속 병렬 수집 (Vercel 10초 타임아웃 방지)
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

    # 4. 제미나이 AI 분석 요청 설정
    system_instruction = """
    당신은 대한민국 주식 시장의 트렌드 변화와 주도 섹터를 포착하는 전문 기관 투자자(프로 트레이더)이자 금융 분석가입니다.
    사용자는 시장 지수 대비 상대강도가 강한 종목을 포착하고, 오랜 횡보나 수렴을 깨고 '새로운 논리(내러티브)'나 '확실한 호재'로 인해 추세 전환 및 신고가를 달성하는 주도주를 매매하는 전략을 가지고 있습니다.

    [Analysis Criteria]
    1. 상승 종목 분석 시: 단순 테마성 순환매인지, 아니면 실적 어닝 서프라이즈, 핵심 수주, 패러다임을 바꾸는 신기술 등 숫자가 찍히거나 연속성이 있는 '새로운 논리'의 등장인지를 명확히 구분하여 기술하세요.
    2. 하락 종목 분석 시: 고점에서의 단순 차익실현(과열 해소)인지, 아니면 업황 악화, 가이던스 하향, 펀더멘털 훼손 같은 '구조적 악재'인지 판단하여 기술하세요.
    3. 경제/금융 전문 용어를 사용하되, 가독성을 위해 불필요한 미사여구와 뻔한 설명("주가는 다양한 요인으로 변동합니다" 등)은 절대 생략하고 팩트 위주로 극도로 압축하세요.

    [Output Format]
    텔레그램 가독성을 위해 반드시 아래 마크다운(Markdown) 양식을 엄격히 준수하여 출력하세요. 종목당 핵심 요약은 최대 2줄을 넘지 않아야 합니다.
    제공된 모든 그룹의 12개 종목을 단 하나도 누락하지 말고 출력 양식 구조에 맞춰 그대로 다 담아내세요.
    """
    
    try:
        model = genai.GenerativeModel(model_name="gemini-1.5-flash", system_instruction=system_instruction)
        response = model.generate_content(
            f"최근 시장 일자: {date_str}\n\n[Input Data]\n{input_data_for_llm}", 
            generation_config={"temperature": 0.2, "top_p": 0.95}
        )
        return response.text
    except Exception as e:
        return f"⚠️ 제미나이 AI 분석 처리 중 오류가 발생했습니다: {str(e)}"

@app.route('/', methods=['POST', 'GET'])
def telegram_webhook():
    if request.method == 'POST':
        update = request.get_json()
        if "message" in update and "text" in update["message"]:
            text = update["message"]["text"]
            chat_id = update["message"]["chat"]["id"]
            
            if text == '/check':
                # 진행 상황 먼저 안내
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={
                    "chat_id": chat_id, 
                    "text": "🔄 당일 거래대금/시총 상위 종목을 스크리닝하고 최신 뉴스 플로우를 분석 중입니다. 약 5~8초 정도 소요됩니다..."
                })
                
                # 주도주 핵심 분석 실행
                analysis_result = run_main_pipeline()
                
                # 분석 결과 최종 발송
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={
                    "chat_id": chat_id,
                    "text": analysis_result,
                    "parse_mode": "Markdown"
                })
        return jsonify({"status": "success"})
    return "Stock Leader Analysis Bot Engine is running!"
