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
    """네이버 금융 시세 테이블 파싱"""
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
