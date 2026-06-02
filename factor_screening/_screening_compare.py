# -*- coding: utf-8 -*-
"""
FINAL.py 수정 없이, 동일 데이터/프레임워크 위에서
스크리닝 방식만 두 가지로 바꾸어 비교만 수행하는 임시 스크립트.

1) T-1주 CP_Q(퀄리티) 스코어 상위 10 Long
2) 12-1M Sharpe 모멘텀 상위 10 Long

리더 팩터별 누적 초과 수익률을 출력한다.
"""
from __future__ import annotations

import os
import sys
import logging

import numpy as np
import pandas as pd
import pymssql

sys.path.append(r"C:\Users\intern9\truston_quant_dev")
from util import variables as v

RELATIVE_UNIVERSE_N = 30
PICK_N = 10
TRANSACTION_COST_ONE_WAY = 0.001

MOMENTUM_LOOKBACK_WEEKS = 52
MOMENTUM_SKIP_WEEKS = 4

IVOL_WINDOW = 36
EWMA_SPAN = 36

DELTA_LONG_FACTORS = {"CP_MOM", "CP_Q", "CP_V", "CP_S"}
IVOL_LONG_FACTORS = {"CP_LV"}
DELTA_SHORT_FACTORS = {"CP_G"}
ACTIVE_FACTORS = DELTA_LONG_FACTORS | IVOL_LONG_FACTORS | DELTA_SHORT_FACTORS

PROJECT_DIR = r"C:\Users\intern9\truston_quant_dev"
OUTPUT_DIR = os.path.join(PROJECT_DIR, "factor_screening", "output")
RANKING_CSV = os.path.join(OUTPUT_DIR, "weekly_factor_ranking.csv")
PRICE_CSV = os.path.join(OUTPUT_DIR, "price_panel_close.csv")
BENCHMARK_XLSX = os.path.join(PROJECT_DIR, "util", "__pycache__", "BM 지수.xlsx")

FACTORS = ["CP_V", "CP_G", "CP_Q", "CP_LV", "CP_MOM", "CP_S"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def fetch_dataframe(query: str) -> pd.DataFrame:
    conn = pymssql.connect(
        server=v.trst_server, user=v.trst_id, password=v.trst_pw,
        database=v.trstdb, charset="utf8",
    )
    try:
        return pd.read_sql(query, conn)
    finally:
        conn.close()


def load_scores(start: str, end: str) -> dict[str, pd.DataFrame]:
    cols = ", ".join(FACTORS)
    query = f"""
    SELECT BaseDate, ComCode, {cols}
    FROM COM_SCORE_QU
    WHERE UnivGroup = 'QU'
      AND BaseDate BETWEEN '{start}' AND '{end}'
    """
    df = fetch_dataframe(query)
    df["BaseDate"] = pd.to_datetime(df["BaseDate"]).dt.normalize()
    df["ComCode"] = df["ComCode"].astype(str).str.strip()
    for f in FACTORS:
        df[f] = pd.to_numeric(df[f], errors="coerce")
    return {
        f: df.pivot_table(index="BaseDate", columns="ComCode", values=f, aggfunc="first")
        for f in FACTORS
    }


def load_sentiment(start: str, end: str) -> pd.DataFrame:
    query = f"""
    SELECT BaseDate, ComCode, CP_SENTIMENT
    FROM COM_SCORE_QU
    WHERE UnivGroup = 'QU'
      AND BaseDate BETWEEN '{start}' AND '{end}'
    """
    df = fetch_dataframe(query)
    df["BaseDate"] = pd.to_datetime(df["BaseDate"]).dt.normalize()
    df["ComCode"] = df["ComCode"].astype(str).str.strip()
    df["CP_SENTIMENT"] = pd.to_numeric(df["CP_SENTIMENT"], errors="coerce")
    return df.pivot_table(
        index="BaseDate", columns="ComCode", values="CP_SENTIMENT", aggfunc="first"
    )


def load_extra_col(start: str, end: str, col: str) -> pd.DataFrame:
    query = f"""
    SELECT BaseDate, ComCode, {col}
    FROM COM_SCORE_QU
    WHERE UnivGroup = 'QU'
      AND BaseDate BETWEEN '{start}' AND '{end}'
    """
    df = fetch_dataframe(query)
    df["BaseDate"] = pd.to_datetime(df["BaseDate"]).dt.normalize()
    df["ComCode"] = df["ComCode"].astype(str).str.strip()
    df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.pivot_table(index="BaseDate", columns="ComCode", values=col, aggfunc="first")


def load_benchmark(dates: list) -> pd.Series:
    raw = pd.read_excel(BENCHMARK_XLSX, header=None)
    data = raw.iloc[14:, [0, 1]].copy()
    data.columns = ["Date", "Close"]
    data["Date"] = pd.to_datetime(data["Date"], errors="coerce")
    data["Close"] = pd.to_numeric(data["Close"], errors="coerce")
    data = data.dropna().set_index("Date").sort_index()
    weekly = (
        data["Close"]
        .reindex(data.index.union(dates))
        .sort_index()
        .ffill(limit=5)
        .reindex(dates)
    )
    weekly.index = pd.Index(dates)
    return weekly


def load_all():
    ranking = pd.read_csv(RANKING_CSV, parse_dates=["BaseDate"])
    ranking["BaseDate"] = ranking["BaseDate"].dt.normalize()
    ranking = ranking.sort_values("BaseDate").reset_index(drop=True)
    dates = ranking["BaseDate"].tolist()
    rank1 = ranking["rank_1"].tolist()

    prices = pd.read_csv(PRICE_CSV, parse_dates=["BaseDate"], index_col="BaseDate")
    prices.index = prices.index.normalize()
    wp = (
        prices.reindex(prices.index.union(dates))
        .sort_index()
        .ffill(limit=5)
        .reindex(dates)
    )
    wp.index = pd.Index(dates)
    priced = set(wp.columns)
    stock_ret = wp.pct_change(fill_method=None)

    bm_weekly = load_benchmark(dates)
    bm_ret = bm_weekly.pct_change()

    scores = load_scores(dates[0].strftime("%Y-%m-%d"), dates[-1].strftime("%Y-%m-%d"))
    sentiment = load_sentiment(dates[0].strftime("%Y-%m-%d"), dates[-1].strftime("%Y-%m-%d"))
    investor = load_extra_col(
        dates[0].strftime("%Y-%m-%d"), dates[-1].strftime("%Y-%m-%d"), "CP_Investor"
    )

    return dates, rank1, wp, stock_ret, bm_ret, scores, sentiment, investor, priced


def pick_by_quality(scores, t_prev, pool, pick_n):
    q = scores.get("CP_Q")
    if q is None or t_prev not in q.index:
        return None
    q_prev = q.loc[t_prev].reindex(pool).dropna()
    if len(q_prev) < pick_n:
        return None
    return q_prev.nlargest(pick_n).index.tolist()


def pick_by_sentiment(sentiment, t_prev, pool, pick_n):
    if t_prev not in sentiment.index:
        return None
    s_prev = sentiment.loc[t_prev].reindex(pool).dropna()
    if len(s_prev) < pick_n:
        return None
    return s_prev.nlargest(pick_n).index.tolist()


def compute_ivol(stock_rets_window, bm_rets_window, pool,
                 min_obs=IVOL_WINDOW, ewma_span=EWMA_SPAN):
    """Expanding window OLS CAPM + EWMA(잔차²) → IVOL."""
    ivol = {}
    bm_valid = bm_rets_window.dropna()
    if len(bm_valid) < min_obs:
        return pd.Series(dtype=float)
    sw = stock_rets_window.reindex(columns=pool)
    for col in sw.columns:
        s = sw[col].dropna()
        common = s.index.intersection(bm_valid.index)
        if len(common) < min_obs:
            continue
        s_c = s.loc[common].values
        m_c = bm_valid.loc[common].values
        m_mean = m_c.mean()
        var_m = ((m_c - m_mean) ** 2).sum()
        if var_m == 0:
            continue
        cov = ((m_c - m_mean) * (s_c - s_c.mean())).sum()
        beta = cov / var_m
        alpha = s_c.mean() - beta * m_mean
        resid = s_c - alpha - beta * m_c
        resid_sq = pd.Series(resid ** 2)
        ewma_var = resid_sq.ewm(span=ewma_span, adjust=False).mean().iloc[-1]
        ivol[col] = float(np.sqrt(ewma_var))
    return pd.Series(ivol)


def pick_by_ivol(stock_ret, bm_ret, k, pool, pick_n):
    stock_win = stock_ret.iloc[:k]
    bm_win = bm_ret.iloc[:k]
    ivol = compute_ivol(stock_win, bm_win, pool)
    if len(ivol) < pick_n:
        return None
    return ivol.nsmallest(pick_n).index.tolist()


def pick_by_topcol(df, t_prev, pool, pick_n):
    if t_prev not in df.index:
        return None
    s_prev = df.loc[t_prev].reindex(pool).dropna()
    if len(s_prev) < pick_n:
        return None
    return s_prev.nlargest(pick_n).index.tolist()


def _avg_rank_cpg(
    scores: dict,
    t_prev: pd.Timestamp,
    pool: list[str],
    lookback: int = 26,
) -> pd.Series | None:
    """
    Past `lookback` 주간 각 주의 cross-sectional CP_G 스코어 랭크를
    종목별로 평균낸 값 (랭크는 ascending: 높은 score → 높은 rank 숫자).
    """
    cpg = scores.get("CP_G")
    if cpg is None or t_prev not in cpg.index:
        return None
    pos = cpg.index.get_loc(t_prev)
    start = max(0, pos - lookback + 1)
    hist = cpg.iloc[start : pos + 1]
    if hist.empty:
        return None
    rank_hist = hist.rank(axis=1, method="average", ascending=True)
    avg_rank = rank_hist.mean(axis=0, skipna=True).reindex(pool)
    return avg_rank


def pick_by_avg_rank_cpg(
    scores: dict,
    t_prev: pd.Timestamp,
    pool: list[str],
    pick_n: int,
    lookback: int = 26,
) -> list[str] | None:
    avg_rank = _avg_rank_cpg(scores, t_prev, pool, lookback)
    if avg_rank is None:
        return None
    avg_rank = avg_rank.dropna()
    if len(avg_rank) < pick_n:
        return None
    return avg_rank.nlargest(pick_n).index.tolist()


def pick_by_combined_rank_cpg(
    scores: dict,
    stock_ret: pd.DataFrame,
    k: int,
    t_prev: pd.Timestamp,
    pool: list[str],
    pick_n: int,
    lookback: int = 26,
    mom_lb: int = 52,
    mom_skip: int = 4,
    w_rank: float = 0.7,
    w_mom: float = 0.3,
) -> list[str] | None:
    """
    Alpha = Rank(w1 * Rank(avg_rank_26w) + w2 * Rank(12-1M return))
    풀(30개) 내에서 두 시그널을 각각 cross-section rank → 가중합 → 다시 rank → 상위 10.
    """
    avg_rank = _avg_rank_cpg(scores, t_prev, pool, lookback)
    if avg_rank is None:
        return None

    start_idx = max(0, k - mom_lb)
    end_idx = k - mom_skip
    if end_idx - start_idx < 12:
        return None
    win = stock_ret.iloc[start_idx:end_idx].reindex(columns=pool)
    ret_12_1 = (1 + win).prod() - 1  # 누적 수익률 (12-1M)

    r1 = avg_rank.rank(method="average", ascending=True)
    r2 = ret_12_1.rank(method="average", ascending=True)
    combined_raw = w_rank * r1 + w_mom * r2
    combined_rank = combined_raw.rank(method="average", ascending=True).dropna()
    if len(combined_rank) < pick_n:
        return None
    return combined_rank.nlargest(pick_n).index.tolist()


def pick_by_sharpe_momentum(stock_ret, k, pool, pick_n):
    start_idx = max(0, k - MOMENTUM_LOOKBACK_WEEKS)
    end_idx = k - MOMENTUM_SKIP_WEEKS
    if end_idx - start_idx < 12:
        return None
    win = stock_ret.iloc[start_idx:end_idx].reindex(columns=pool)
    mu = win.mean()
    sigma = win.std()
    sharpe = (mu / sigma.replace(0, np.nan)).dropna()
    if len(sharpe) < pick_n:
        return None
    return sharpe.nlargest(pick_n).index.tolist()


def run_mode(mode, dates, rank1, wp, stock_ret, bm_ret, scores, sentiment, investor, priced):
    tx = TRANSACTION_COST_ONE_WAY
    start_k = MOMENTUM_LOOKBACK_WEEKS + 1  # 두 모드 공통 비교 가능하도록
    records = []
    prev_picks: set[str] = set()

    for k in range(start_k, len(dates)):
        t_prev, t = dates[k - 1], dates[k]
        leader = rank1[k]
        if leader not in ACTIVE_FACTORS:
            continue

        all_sc = pd.DataFrame(
            {
                f: scores[f].loc[t_prev] if t_prev in scores[f].index else pd.Series(dtype=float)
                for f in FACTORS
            }
        )
        all_sc = all_sc.dropna(how="all").loc[lambda x: x.index.isin(priced)]

        others = [f for f in FACTORS if f != leader]
        rel = (all_sc[leader] - all_sc[others].mean(axis=1)).dropna()
        if len(rel) < RELATIVE_UNIVERSE_N:
            continue
        pool = rel.nlargest(RELATIVE_UNIVERSE_N).index.tolist()

        if mode == "quality":
            picks = pick_by_quality(scores, t_prev, pool, PICK_N)
        elif mode == "momentum":
            picks = pick_by_sharpe_momentum(stock_ret, k, pool, PICK_N)
        elif mode == "sentiment":
            picks = pick_by_sentiment(sentiment, t_prev, pool, PICK_N)
        elif mode == "investor":
            picks = pick_by_topcol(investor, t_prev, pool, PICK_N)
        elif mode == "ivol_g":
            if leader != "CP_G":
                continue
            picks = pick_by_ivol(stock_ret, bm_ret, k, pool, PICK_N)
        elif mode == "avgrank_g":
            if leader != "CP_G":
                continue
            picks = pick_by_avg_rank_cpg(scores, t_prev, pool, PICK_N)
        elif mode == "combined_g":
            if leader != "CP_G":
                continue
            picks = pick_by_combined_rank_cpg(
                scores, stock_ret, k, t_prev, pool, PICK_N
            )
        else:
            raise ValueError(mode)
        if picks is None:
            continue

        fwd = (
            (wp.loc[t] / wp.loc[t_prev] - 1)
            .reindex(picks)
            .replace([np.inf, -np.inf], np.nan)
            .dropna()
        )
        if fwd.empty:
            continue

        picks_set = set(picks)
        entries = picks_set - prev_picks
        exits = prev_picks - picks_set
        n_traded = len(entries) + len(exits)
        cost = n_traded * tx / PICK_N

        basket = float(fwd.mean()) - cost  # 항상 Long

        bmr = bm_ret.iloc[k] if k < len(bm_ret) else 0.0
        if pd.isna(bmr):
            bmr = 0.0

        records.append(
            {
                "BaseDate": t,
                "year": t.year,
                "leader": leader,
                "basket_return": basket,
                "bm_return": bmr,
                "excess": basket - bmr,
            }
        )
        prev_picks = picks_set

    return pd.DataFrame(records)


def report(weekly: pd.DataFrame, label: str) -> None:
    print("=" * 80)
    print(f"  {label}")
    print("=" * 80)

    if weekly.empty:
        print("  (결과 없음)")
        return

    weekly = weekly.sort_values("BaseDate").reset_index(drop=True)
    strat_cum = (1 + weekly["basket_return"]).prod() - 1
    bm_cum = (1 + weekly["bm_return"]).prod() - 1
    excess_cum = strat_cum - bm_cum

    eq_strat = (1 + weekly["basket_return"]).cumprod()
    eq_bm = (1 + weekly["bm_return"]).cumprod()
    strat_mdd = float((eq_strat / eq_strat.cummax() - 1).min())
    bm_mdd = float((eq_bm / eq_bm.cummax() - 1).min())
    years = len(weekly) / 52.0
    strat_cagr = (1 + strat_cum) ** (1 / years) - 1 if years > 0 else 0
    bm_cagr = (1 + bm_cum) ** (1 / years) - 1 if years > 0 else 0

    print(f"  기간                      : {weekly['BaseDate'].iloc[0].date()} ~ {weekly['BaseDate'].iloc[-1].date()}")
    print(f"  주 수                     : {len(weekly)}")
    print(f"  전략 누적 수익률          : {strat_cum*100:.2f}%")
    print(f"  BM   누적 수익률          : {bm_cum*100:.2f}%")
    print(f"  전체 누적 초과 수익률     : {excess_cum*100:.2f}%")
    print(f"  전략 CAGR / MDD           : {strat_cagr*100:.2f}% / {strat_mdd*100:.2f}%")
    print(f"  BM   CAGR / MDD           : {bm_cagr*100:.2f}% / {bm_mdd*100:.2f}%")
    win_rate = (weekly["basket_return"] > 0).mean()
    beat_bm_rate = (weekly["basket_return"] > weekly["bm_return"]).mean()
    print(f"  승률 (basket > 0)         : {win_rate*100:.2f}%")
    print(f"  BM 대비 승률 (basket > BM): {beat_bm_rate*100:.2f}%")
    print()

    print("  --- Leader 팩터별 누적 초과 수익률 ---")
    rows = []
    for leader, g in weekly.groupby("leader"):
        s = (1 + g["basket_return"]).prod() - 1
        b = (1 + g["bm_return"]).prod() - 1
        rows.append(
            {
                "leader": leader,
                "n_weeks": len(g),
                "strat_cum(%)": round(s * 100, 2),
                "bm_cum(%)": round(b * 100, 2),
                "cum_excess(%)": round((s - b) * 100, 2),
            }
        )
    factor_df = pd.DataFrame(rows).set_index("leader").sort_values("cum_excess(%)", ascending=False)
    print(factor_df.to_string())
    print()


def main() -> None:
    dates, rank1, wp, stock_ret, bm_ret, scores, sentiment, investor, priced = load_all()

    mode_sel = os.environ.get("MODE", "all")
    all_modes = [
        ("quality", "Method 1: T-1주 CP_Q(퀄리티) 스코어 상위 10 Long"),
        ("momentum", "Method 2: 12-1M Sharpe Momentum 상위 10 Long"),
        ("sentiment", "Method 3: T-1주 CP_SENTIMENT 스코어 상위 10 Long"),
        ("investor", "Method 4: T-1주 CP_Investor 스코어 상위 10 Long"),
        ("ivol_g", "Method 5: CP_G 리더 주만, IVOL 하위 10 Long (expanding CAPM + EWMA)"),
        (
            "avgrank_g",
            "Method 6: CP_G 리더 주만, 6개월(26주) 평균 CP_G 랭크 상위 10 Long",
        ),
        (
            "combined_g",
            "Method 7: CP_G 리더 주만, Rank(0.7·Rank(6M AvgRank) + 0.3·Rank(12-1M Return)) 상위 10 Long",
        ),
    ]
    selected = all_modes if mode_sel == "all" else [m for m in all_modes if m[0] == mode_sel]

    for mode, label in selected:
        weekly = run_mode(
            mode, dates, rank1, wp, stock_ret, bm_ret, scores, sentiment, investor, priced
        )
        report(weekly, label)


if __name__ == "__main__":
    main()
