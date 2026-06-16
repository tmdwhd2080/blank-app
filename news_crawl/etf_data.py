

\
\
\
\
\
\
\
\
   

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

from news_crawl.models import Constituent, ETFFeature, SentimentScore, Symbol
from news_crawl.utils import parse_float, parse_int, previous_business_day
from trading.kis import KisClient, KisError
from trading.kis.stocks import (
    etf_component_stocks,
    etf_inquire_price,
    inquire_daily_chart,
    inquire_index_daily_chart,
)


                                           
DEFAULT_ETFS: list[tuple[str, str]] = [
    ("069500", "KODEX 200"),
    ("102110", "TIGER 200"),
    ("122630", "KODEX 레버리지"),
    ("252670", "KODEX 200선물인버스2X"),
    ("229200", "KODEX 코스닥150"),
    ("233740", "KODEX 코스닥150레버리지"),
    ("360750", "TIGER 미국S&P500"),
    ("379800", "KODEX 미국S&P500"),
    ("133690", "TIGER 미국나스닥100"),
    ("379810", "KODEX 미국나스닥100"),
    ("305720", "KODEX 2차전지산업"),
    ("305540", "TIGER 2차전지테마"),
    ("091160", "KODEX 반도체"),
    ("091170", "KODEX 은행"),
    ("117460", "KODEX 에너지화학"),
    ("139660", "TIGER 200 IT"),
    ("228790", "TIGER 화장품"),
    ("143860", "TIGER 헬스케어"),
    ("305080", "TIGER 미국채10년선물"),
    ("214980", "KODEX 단기채권PLUS"),
    ("273130", "KODEX 종합채권"),
    ("278530", "KODEX 200TR"),
    ("294400", "KOSEF 200TR"),
    ("411060", "ACE 미국빅테크TOP7Plus"),
    ("449450", "PLUS K방산"),
]


INVESTOR_PATH = "/uapi/domestic-stock/v1/quotations/inquire-investor"
INVESTOR_TR_ID = "FHKST01010900"


def _master_etf_universe(limit: int) -> list[Symbol]:
                                                        
    try:
        from trading.kis_kospi_large_caps import download_master_rows, parse_master
    except Exception:
        return []
    try:
        df = parse_master(download_master_rows())
    except Exception:
        return []
    etfs = df[df["그룹코드"] == "EF"].copy()
    if etfs.empty:
        return []
    import pandas as pd

    etfs["_vol"] = pd.to_numeric(etfs.get("전일거래량"), errors="coerce").fillna(0)
    etfs = etfs.sort_values("_vol", ascending=False)
    symbols = [
        Symbol(code=str(row["단축코드"]).strip(), name=str(row["한글명"]).strip(), market="ETF")
        for _, row in etfs.iterrows()
        if str(row["단축코드"]).strip().isdigit() and len(str(row["단축코드"]).strip()) == 6
    ]
    return symbols[:limit] if limit and limit > 0 else symbols


def etf_universe(*, limit: int = 60) -> list[Symbol]:
    symbols = _master_etf_universe(limit)
    if symbols:
        return symbols
    fallback = [Symbol(code=code, name=name, market="ETF") for code, name in DEFAULT_ETFS]
    return fallback[:limit] if limit and limit > 0 else fallback


def symbols_from_codes(codes: list[str]) -> list[Symbol]:
    names = {code: name for code, name in DEFAULT_ETFS}
    out: list[Symbol] = []
    for raw in codes:
        code = raw.strip()
        if code:
            out.append(Symbol(code=code, name=names.get(code, code), market="ETF"))
    return out


def _date_str(day: datetime) -> str:
    return day.strftime("%Y%m%d")


def _history_closes(
    client: KisClient,
    code: str,
    *,
    history_days: int,
    as_of: datetime | None,
) -> list[float]:
    end = (as_of or datetime.now()).date()
    if as_of is not None:
        end = previous_business_day(end)
    start = end - timedelta(days=int(history_days * 1.7) + 10)
    try:
        rows = inquire_daily_chart(
            client,
            code,
            start=_date_str(datetime.combine(start, datetime.min.time())),
            end=_date_str(datetime.combine(end, datetime.min.time())),
        )
    except KisError:
        return []
                               
    parsed: list[tuple[str, float]] = []
    for row in rows:
        day = str(row.get("stck_bsop_date") or "")
        close = parse_float(row.get("stck_clpr"))
        if not day or close is None:
            continue
        if as_of is not None and day > _date_str(datetime.combine(end, datetime.min.time())):
            continue
        parsed.append((day, close))
    parsed.sort(key=lambda item: item[0])
    closes = [close for _, close in parsed]
    return closes[-history_days:] if history_days > 0 else closes


def _supply_demand(
    client: KisClient,
    code: str,
    *,
    days: int,
    as_of: datetime | None,
) -> tuple[int, int, int, float]:
                                                  
    try:
        data = client.get(
            INVESTOR_PATH,
            tr_id=INVESTOR_TR_ID,
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
        )
    except KisError:
        return 0, 0, 0, 0.0
    rows = data.get("output") or data.get("output1") or []
    if isinstance(rows, dict):
        rows = [rows]
    cutoff = None
    if as_of is not None:
        cutoff = _date_str(datetime.combine(previous_business_day(as_of.date()), datetime.min.time()))

    used = 0
    frgn = inst = indiv = 0
    frgn_amt = inst_amt = 0.0
    for row in rows:
        day = str(row.get("stck_bsop_date") or "")
        if cutoff is not None and day > cutoff:
            continue
        if used >= days > 0:
            break
        used += 1
        frgn += parse_int(row.get("frgn_ntby_qty")) or 0
        inst += parse_int(row.get("orgn_ntby_qty")) or 0
        indiv += parse_int(row.get("prsn_ntby_qty")) or 0
        frgn_amt += parse_float(row.get("frgn_ntby_tr_pbmn")) or 0.0
        inst_amt += parse_float(row.get("orgn_ntby_tr_pbmn")) or 0.0

                                                
    import math

    smart_amount = frgn_amt + inst_amt
    score = math.tanh(smart_amount / 50_000.0)               
    return frgn, inst, indiv, score


def _constituents(client: KisClient, code: str, *, top: int = 10) -> list[Constituent]:
    try:
        rows = etf_component_stocks(client, code)
    except KisError:
        return []
    out: list[Constituent] = []
    for row in rows:
        sub = str(row.get("stck_shrn_iscd") or row.get("stck_cntg_iscd") or "").strip()
        name = str(row.get("hts_kor_isnm") or "").strip()
        if not sub:
            continue
        out.append(
            Constituent(
                code=sub,
                name=name,
                weight_pct=parse_float(row.get("etf_cnfg_issu_rlim")),
                change_pct=parse_float(row.get("prdy_ctrt")),
            )
        )
    out.sort(key=lambda c: (c.weight_pct or 0.0), reverse=True)
    return out[:top]


def _momentum_1w(closes: list[float]) -> float | None:
    week = closes[-6:]
    if len(week) < 2 or week[0] == 0:
        return None
    return week[-1] / week[0] - 1.0


def collect_etf_feature(
    client: KisClient,
    symbol: Symbol,
    score: SentimentScore | None,
    *,
    history_days: int = 90,
    supply_demand_days: int = 5,
    as_of: datetime | None = None,
) -> ETFFeature:
    closes = _history_closes(client, symbol.code, history_days=history_days, as_of=as_of)
    weekly = closes[-6:]

    nav = prev_nav = nav_change = premium = tracking = net_assets = None
    current_price = today_open = None
    try:
        out = etf_inquire_price(client, symbol.code).get("output") or {}
        current_price = parse_float(out.get("stck_prpr"))
        today_open = parse_float(out.get("stck_oprc"))
        nav = parse_float(out.get("nav"))
        prev_nav = parse_float(out.get("prdy_last_nav"))
        nav_change = parse_float(out.get("nav_prdy_ctrt"))
        tracking = parse_float(out.get("trc_errt"))
        net_assets = parse_float(out.get("etf_ntas_ttam"))
        if current_price and nav:
            premium = (current_price - nav) / nav * 100.0
    except KisError:
        pass

    frgn, inst, indiv, sd_score = _supply_demand(
        client, symbol.code, days=supply_demand_days, as_of=as_of
    )
    constituents = _constituents(client, symbol.code)

    if current_price is None and closes:
        current_price = closes[-1]

    return ETFFeature(
        code=symbol.code,
        name=symbol.name,
        market=symbol.market,
        current_price=current_price,
        today_open=today_open,
        nav=nav,
        prev_nav=prev_nav,
        nav_change_pct=nav_change,
        premium_discount_pct=premium,
        tracking_error_pct=tracking,
        net_assets=net_assets,
        weekly_closes=weekly,
        history_closes=closes,
        momentum_1w=_momentum_1w(closes),
        foreign_net_qty=frgn,
        institution_net_qty=inst,
        individual_net_qty=indiv,
        supply_demand_score=sd_score,
        supply_demand_days=supply_demand_days,
        constituents=constituents,
        s_score=score.s_score if score else 0.0,
        s_confidence=score.confidence if score else 0.0,
        s_model=score.model if score else "analysis",
        news_count=score.news_count if score else 0,
    )


def collect_etf_features(
    client: KisClient,
    symbols: Iterable[Symbol],
    scores: dict[str, SentimentScore],
    *,
    history_days: int = 90,
    supply_demand_days: int = 5,
    as_of: datetime | None = None,
) -> list[ETFFeature]:
    features: list[ETFFeature] = []
    for symbol in symbols:
        try:
            features.append(
                collect_etf_feature(
                    client,
                    symbol,
                    scores.get(symbol.code),
                    history_days=history_days,
                    supply_demand_days=supply_demand_days,
                    as_of=as_of,
                )
            )
        except Exception:
            continue
    return features


def kospi_index_closes(
    client: KisClient,
    *,
    history_days: int = 90,
    as_of: datetime | None = None,
) -> list[float]:
    end = (as_of or datetime.now()).date()
    if as_of is not None:
        end = previous_business_day(end)
    start = end - timedelta(days=int(history_days * 1.7) + 10)
    try:
        rows = inquire_index_daily_chart(
            client,
            "0001",
            start=_date_str(datetime.combine(start, datetime.min.time())),
            end=_date_str(datetime.combine(end, datetime.min.time())),
        )
    except KisError:
        return []
    parsed: list[tuple[str, float]] = []
    for row in rows:
        day = str(row.get("stck_bsop_date") or "")
        close = parse_float(row.get("bstp_nmix_prpr"))
        if day and close is not None:
            parsed.append((day, close))
    parsed.sort(key=lambda item: item[0])
    closes = [close for _, close in parsed]
    return closes[-history_days:] if history_days > 0 else closes
