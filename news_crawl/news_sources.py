                       

from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests

from news_crawl.models import NewsItem, Symbol
from news_crawl.utils import strip_html


class NewsSourceError(RuntimeError):
    pass


class NaverFinanceNewsClient:

    endpoint = "https://finance.naver.com/item/news_news.naver"

    def __init__(self, *, timeout: float = 10.0, max_pages: int = 5) -> None:
        self.timeout = timeout
        self.max_pages = max_pages
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                ),
                "Referer": "https://finance.naver.com/",
            }
        )

    def fetch_symbol_news(
        self,
        symbol: Symbol,
        *,
        window_start: datetime,
        window_end: datetime,
        max_items: int = 8,
    ) -> list[NewsItem]:
        items: list[NewsItem] = []
        seen: set[str] = set()
        tz = window_start.tzinfo or ZoneInfo("Asia/Seoul")
        stop = False

        for page in range(1, self.max_pages + 1):
            rows = self._fetch_page(symbol.code, page)
            if not rows:
                break
            for row in rows:
                published = row["published_at"].replace(tzinfo=tz)
                if published > window_end:
                    continue
                if published < window_start:
                    stop = True
                    continue
                key = row["link"] or row["title"]
                if key in seen:
                    continue
                seen.add(key)
                items.append(
                    NewsItem(
                        code=symbol.code,
                        name=symbol.name,
                        title=row["title"],
                        description=row["source"],
                        origin=row["source"],
                        link=row["link"],
                        published_at=published,
                        source="naver_finance",
                    )
                )
                if len(items) >= max_items:
                    return items
            if stop:
                break
        return items

    def _fetch_page(self, code: str, page: int) -> list[dict]:
        params = {
            "code": code,
            "page": page,
            "sm": "title_entity_id.basic",
            "clusterId": "",
        }
        try:
            response = self.session.get(self.endpoint, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise NewsSourceError(f"Naver Finance request failed: {exc}") from exc
        if response.status_code >= 400:
            raise NewsSourceError(f"Naver Finance HTTP {response.status_code}: {response.text[:500]}")
        response.encoding = response.apparent_encoding or "euc-kr"
        return _parse_finance_news_table(response.text)


def _parse_finance_news_table(html_text: str) -> list[dict]:
    rows: list[dict] = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html_text, flags=re.I | re.S):
        if "class=\"title\"" not in tr and "class='title'" not in tr:
            continue
        title_match = re.search(
            r"<td[^>]*class=[\"']title[\"'][^>]*>.*?<a[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
            tr,
            flags=re.I | re.S,
        )
        date_match = re.search(
            r"<td[^>]*class=[\"']date[\"'][^>]*>(.*?)</td>",
            tr,
            flags=re.I | re.S,
        )
        if not title_match or not date_match:
            continue
        source_match = re.search(
            r"<td[^>]*class=[\"']info[\"'][^>]*>(.*?)</td>",
            tr,
            flags=re.I | re.S,
        )
        title = strip_html(title_match.group(2))
        date_text = strip_html(date_match.group(1))
        try:
            published = datetime.strptime(date_text, "%Y.%m.%d %H:%M")
        except ValueError:
            continue
        rows.append(
            {
                "title": title,
                "source": strip_html(source_match.group(1)) if source_match else "",
                "link": urljoin("https://finance.naver.com", title_match.group(1)),
                "published_at": published,
            }
        )
    return rows


def fetch_naver_finance_news_for_universe(
    client: NaverFinanceNewsClient,
    symbols: Iterable[Symbol],
    *,
    window_start: datetime,
    window_end: datetime,
    max_items: int = 8,
) -> dict[str, list[NewsItem]]:
    result: dict[str, list[NewsItem]] = {}
    for symbol in symbols:
        try:
            result[symbol.code] = client.fetch_symbol_news(
                symbol,
                window_start=window_start,
                window_end=window_end,
                max_items=max_items,
            )
        except NewsSourceError:
            result[symbol.code] = []
    return result
