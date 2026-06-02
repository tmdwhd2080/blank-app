# -*- coding: utf-8 -*-
"""
SymbolSearch — 키워드로 종목/ETF 검색
======================================
전체 종목을 매번 받아오는 건 낭비라 로컬 CSV 캐시 (~/.kiwoom/symbols.csv) 에
저장하고 검색은 메모리에서 처리.

  - 캐시 미존재 / N일 경과 → 자동 재다운로드
  - --refresh 로 강제 재다운로드
  - 한글/영문/공백 무시 부분문자열 매칭
  - 정확매칭 > prefix > substring 순으로 정렬
"""

from __future__ import annotations

import csv
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from trading.kiwoom.http_client import KiwoomClient
from trading.kiwoom.tr import listing


log = logging.getLogger(__name__)


DEFAULT_MARKETS = ("kospi", "kosdaq", "etf")


@dataclass(frozen=True)
class Symbol:
    code: str
    name: str
    market: str

    @property
    def name_norm(self) -> str:
        return _normalize_text(self.name)


# ============================================================
#  검색 클래스
# ============================================================


class SymbolSearch:
    def __init__(
        self,
        client: KiwoomClient | None = None,
        cache_path: Path | None = None,
        ttl_days: int = 7,
    ):
        self._client = client
        self._cache = cache_path or (Path.home() / ".kiwoom" / "symbols.csv")
        self._ttl = timedelta(days=ttl_days)
        self._symbols: list[Symbol] | None = None

    # --------------------------------------------------------
    #  공개 API
    # --------------------------------------------------------

    def search(
        self,
        keyword: str,
        market: str | None = None,
        limit: int | None = 50,
    ) -> list[Symbol]:
        """키워드 검색.

        - keyword 가 6자리 숫자: 종목코드 정확 매칭
        - keyword 가 1~5자리 숫자: 종목코드 prefix 매칭
        - 그 외: 이름 부분문자열 매칭

        market: 'kospi' / 'kosdaq' / 'etf' 로 좁히려면 지정.
        limit: None 이면 전체 매칭 반환.
        """
        symbols = self._ensure_loaded()
        if market:
            label = market.upper()
            symbols = [s for s in symbols if s.market == label]

        kw = keyword.strip()
        if not kw:
            return []

        # 종목코드 (숫자만)
        if kw.isdigit():
            if len(kw) == 6:
                results = [s for s in symbols if s.code == kw]
            else:
                results = [s for s in symbols if s.code.startswith(kw)]
            return results if limit is None else results[:limit]

        # 이름 검색
        kw_norm = _normalize_text(kw)
        scored: list[tuple[int, Symbol]] = []
        for s in symbols:
            n = s.name_norm
            if kw_norm not in n:
                continue
            if n == kw_norm:
                score = 0
            elif n.startswith(kw_norm):
                score = 1
            else:
                score = 2
            scored.append((score, s))

        scored.sort(key=lambda x: (x[0], x[1].name))
        out = [s for _, s in scored]
        return out if limit is None else out[:limit]

    def count(
        self,
        keyword: str,
        market: str | None = None,
    ) -> int:
        """검색에 매칭되는 전체 개수 (limit 적용 전)."""
        return len(self.search(keyword, market=market, limit=None))

    def stats(self) -> dict[str, int]:
        """캐시 안의 시장별 종목 수. 캐시가 비정상일 때 진단용."""
        counts: dict[str, int] = {}
        for s in self._ensure_loaded():
            counts[s.market] = counts.get(s.market, 0) + 1
        counts["TOTAL"] = sum(counts.values())
        return counts

    def force_add(self, code: str) -> Symbol | None:
        """전종목 리스트에 누락된 종목을 ka10001 (주식기본정보) 로 직접 받아 캐시에 추가.

        ka10099 가 일부 ETF/특수종목을 안 돌려주는 경우 사용.
        """
        if self._client is None:
            self._client = KiwoomClient()

        try:
            resp = self._client.call("ka10001", {"stk_cd": code})
        except Exception as e:
            log.warning("force_add(%s) failed: %s", code, e)
            return None

        body = resp.body
        name = (
            body.get("stk_nm")
            or body.get("name")
            or body.get("isu_nm")
            or ""
        ).strip()
        if not name:
            log.warning("force_add(%s): 응답에 종목명 필드 없음 — body=%s", code, body)
            return None

        sym = Symbol(code=code, name=name, market="EXTRA")
        self._ensure_loaded()
        # 중복 제거
        self._symbols = [s for s in (self._symbols or []) if s.code != code]
        self._symbols.append(sym)
        self._write_cache(self._symbols)
        log.info("force_add: %s → %s", code, name)
        return sym

    def _write_cache(self, symbols: list[Symbol]) -> None:
        self._cache.parent.mkdir(parents=True, exist_ok=True)
        with self._cache.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["code", "name", "market"])
            w.writeheader()
            for s in symbols:
                w.writerow({"code": s.code, "name": s.name, "market": s.market})

    def refresh(self, markets: tuple[str, ...] = DEFAULT_MARKETS) -> int:
        """API 에서 시장별로 다운로드해 캐시 새로 저장. 받은 종목 수 반환."""
        if self._client is None:
            self._client = KiwoomClient()

        all_rows: list[dict] = []
        for m in markets:
            log.info("downloading %s ...", m)
            try:
                rows = listing.stock_list(self._client, market=m)
                log.info("  → %d rows", len(rows))
                all_rows.extend(rows)
            except Exception as e:
                log.warning("  failed: %s", e)
            time.sleep(0.3)  # 레이트리밋 여유

        # 중복 제거 (동일 종목코드)
        seen: set[str] = set()
        deduped: list[dict] = []
        for r in all_rows:
            if r["code"] and r["code"] not in seen:
                seen.add(r["code"])
                deduped.append(r)

        self._symbols = [Symbol(**r) for r in deduped]
        self._write_cache(self._symbols)
        log.info("cache saved: %s (%d unique symbols)", self._cache, len(deduped))
        return len(deduped)

    def lookup(self, code: str) -> Symbol | None:
        """종목코드 → Symbol. 잔고 조회 결과의 stk_cd 로 종목명 보강할 때."""
        for s in self._ensure_loaded():
            if s.code == code:
                return s
        return None

    # --------------------------------------------------------
    #  내부
    # --------------------------------------------------------

    def _ensure_loaded(self) -> list[Symbol]:
        if self._symbols is not None:
            return self._symbols
        if self._cache.exists() and not self._is_stale():
            self._symbols = list(self._read_cache())
            return self._symbols
        # 캐시 없거나 오래됨 → 갱신
        self.refresh()
        return self._symbols or []

    def _is_stale(self) -> bool:
        mtime = datetime.fromtimestamp(self._cache.stat().st_mtime)
        return datetime.now() - mtime > self._ttl

    def _read_cache(self):
        with self._cache.open(encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                yield Symbol(
                    code=row["code"],
                    name=row["name"],
                    market=row["market"],
                )


# ============================================================
#  헬퍼
# ============================================================


def _normalize_text(s: str) -> str:
    """검색 비교용 정규화: 소문자 + 공백/하이픈 제거."""
    return "".join(s.lower().split()).replace("-", "").replace("_", "")
