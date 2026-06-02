# [로컬 PC용 크롤링 코드] Jupyter Notebook이나 VS Code에서 실행하세요!
import requests
from bs4 import BeautifulSoup
import pandas as pd
import time
from tqdm import tqdm # 코랩이 아니므로 .notebook을 뺍니다.

print("🚀 [로컬 크롤링] 네이버 뉴스 헤드라인 수집기")

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

keywords = ["금리 인상", "CPI", "인플레이션", "비트코인 현물 ETF", "가상자산 규제"]
start_date_str = '2023.01.01'
end_date_str = '2023.01.31'
MAX_PAGES = 5

def crawl_naver_news_local(keyword, start_date, end_date, max_pages):
    news_list = []
    page_starts = [1 + (i * 10) for i in range(max_pages)]

    print(f"\n🔍 '{keyword}' 검색 시작")

    for start in tqdm(page_starts, desc=f"{keyword} 수집 중"):
        url = "https://search.naver.com/search.naver"
        params = {
            "where": "news", "query": keyword, "pd": "3",
            "ds": start_date, "de": end_date,
            "nso": f"so:r,p:from{start_date.replace('.','')}to{end_date.replace('.','')}",
            "start": start
        }

        try:
            response = requests.get(url, params=params, headers=headers)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')

            articles = soup.select('a.news_tit')
            dates = soup.select('div.info_group > span.info')

            if len(articles) == 0: break

            for i, article in enumerate(articles):
                title = article.get('title')
                link = article.get('href')
                date_text = dates[i].text.strip() if i < len(dates) else "알수없음"

                news_list.append({
                    "Date": date_text, "Keyword": keyword, "Headline": title, "URL": link
                })
            time.sleep(1.0) # 개인 IP라도 너무 빠르면 차단되니 1초 휴식 필수!
        except Exception as e:
            print(f"오류 발생: {e}")
            break
    return news_list

all_news_data = []
for kw in keywords:
    all_news_data.extend(crawl_naver_news_local(kw, start_date_str, end_date_str, MAX_PAGES))

df_news = pd.DataFrame(all_news_data)

if len(df_news) > 0:
    # 💡 내 컴퓨터(현재 폴더)에 CSV 파일로 저장
    save_path = 'crawled_news_sample.csv'
    df_news.to_csv(save_path, index=False, encoding='utf-8-sig')
    print(f"\n✅ 로컬 저장 완료: {save_path} (파일을 확인하세요!)")
    
#python sfd.py