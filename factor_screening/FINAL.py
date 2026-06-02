# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys
import logging

import numpy as np
import pandas as pd
import pymssql
import matplotlib
import matplotlib.pyplot as plt

sys.path.append(r"C:\Users\intern9\truston_quant_dev")
from util import variables as v  #

# SETTINGS
RELATIVE_UNIVERSE_N = 30 # 상대 팩터 스코어로 몇개를 고를지
PICK_N = 10 # 최종적으로 몇개를 고를지
TRANSACTION_COST_ONE_WAY = 0.001  # 편도 거래 비용 (바뀐 종목에만 적용)

# 스크리닝 방식 분기
DELTA_FACTORS = {"CP_S", "CP_G", "CP_V"}               # Δscore(T-1 − T-2) 상위 10 Long
SENTIMENT_FACTORS = {"CP_MOM", "CP_LV", "CP_Q"}        # CP_SENTIMENT(T-1) 상위 10 Long
ACTIVE_FACTORS = DELTA_FACTORS | SENTIMENT_FACTORS

PROJECT_DIR = r"C:\Users\intern9\truston_quant_dev"
OUTPUT_DIR = os.path.join(PROJECT_DIR, "factor_screening", "output")
RANKING_CSV = os.path.join(OUTPUT_DIR, "weekly_factor_ranking.csv")
PRICE_CSV = os.path.join(OUTPUT_DIR, "price_panel_close.csv")
BENCHMARK_XLSX = os.path.join(PROJECT_DIR, "util", "__pycache__", "BM 지수.xlsx")

FACTORS = ["CP_V", "CP_G", "CP_Q", "CP_LV", "CP_MOM", "CP_S"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# 데이터 로드
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


# main 전략 로직
def run_strategy() -> pd.DataFrame:
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

    bm_weekly = load_benchmark(dates)
    bm_ret = bm_weekly.pct_change()

    scores = load_scores(dates[0].strftime("%Y-%m-%d"), dates[-1].strftime("%Y-%m-%d"))
    sentiment = load_sentiment(dates[0].strftime("%Y-%m-%d"), dates[-1].strftime("%Y-%m-%d"))

    tx = TRANSACTION_COST_ONE_WAY
    start_k = 2
    records: list[dict] = []

    prev_picks: set[str] = set()
    prev_direction: str | None = None

    for k in range(start_k, len(dates)):
        t_prev, t = dates[k - 1], dates[k]
        t_2 = dates[k - 2]
        leader = rank1[k]
        if leader not in ACTIVE_FACTORS:
            continue

        sdf = scores.get(leader)
        if sdf is None or t_prev not in sdf.index:
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

        # 종목 선택
        if leader in DELTA_FACTORS:
            # Δscore(T-1 − T-2) 상위 10 Long  (CP_S, CP_G, CP_V, CP_Q)
            if t_2 not in sdf.index:
                continue
            s2 = sdf.loc[t_2].reindex(pool)
            s1 = sdf.loc[t_prev].reindex(pool)
            delta = (s1 - s2).dropna()
            if len(delta) < PICK_N:
                continue
            picks = delta.nlargest(PICK_N).index.tolist()
        else:
            # CP_SENTIMENT(T-1) 상위 10 Long  (CP_MOM, CP_LV)
            if t_prev not in sentiment.index:
                continue
            sent = sentiment.loc[t_prev].reindex(pool).dropna()
            if len(sent) < PICK_N:
                continue
            picks = sent.nlargest(PICK_N).index.tolist()
        direction = "long"

        fwd = (
            (wp.loc[t] / wp.loc[t_prev] - 1)
            .reindex(picks)
            .replace([np.inf, -np.inf], np.nan)
            .dropna()
        )
        if fwd.empty:
            continue

        # === 거래비용: 바뀐 종목(엔트리 + 엑시트)에만 편도 비용 적용 ===
        picks_set = set(picks)
        if prev_direction is not None and prev_direction == direction:
            entries = picks_set - prev_picks
            exits = prev_picks - picks_set
        else:
            # 방향 전환 또는 최초 거래
            entries = picks_set
            exits = prev_picks if prev_direction is not None else set()
        n_traded = len(entries) + len(exits)
        # 각 종목은 포트폴리오의 1/PICK_N 비중 → 가중 비용
        cost = n_traded * tx / PICK_N

        if direction == "long":
            basket = float(fwd.mean()) - cost
        else:
            basket = -float(fwd.mean()) - cost

        bmr = bm_ret.iloc[k] if k < len(bm_ret) else 0.0
        if pd.isna(bmr):
            bmr = 0.0

        records.append(
            {
                "BaseDate": t,
                "year": t.year,
                "leader": leader,
                "direction": direction,
                "basket_return": basket,
                "bm_return": bmr,
                "excess": basket - bmr,
                "beat_bm": int(basket > bmr),
                "win": int(basket > 0),
                "n_traded": n_traded,
                "cost_applied": cost,
            }
        )

        prev_picks = picks_set
        prev_direction = direction

    return pd.DataFrame(records)


# 리포트
def report(weekly: pd.DataFrame) -> None:
    weekly = weekly.sort_values("BaseDate").reset_index(drop=True)

    # 연도별 초과 수익률
    yearly_excess = weekly.groupby("year").apply(
        lambda g: ((1 + g["basket_return"]).prod() - 1) - ((1 + g["bm_return"]).prod() - 1),
        include_groups=False,
    ).mul(100)

    # 리더 팩터별 누적 초과 수익률
    factor_rows: list[dict] = []
    for leader, g in weekly.groupby("leader"):
        s_cum = (1 + g["basket_return"]).prod() - 1
        b_cum = (1 + g["bm_return"]).prod() - 1
        factor_rows.append(
            {
                "leader": leader,
                "n_weeks": len(g),
                "strat_cum(%)": round(s_cum * 100, 2),
                "bm_cum(%)": round(b_cum * 100, 2),
                "cum_excess(%)": round((s_cum - b_cum) * 100, 2),
            }
        )
    factor_excess = (
        pd.DataFrame(factor_rows)
        .set_index("leader")
        .sort_values("cum_excess(%)", ascending=False)
    )

    # 전체 지표
    strat_cum = (1 + weekly["basket_return"]).prod() - 1
    bm_cum = (1 + weekly["bm_return"]).prod() - 1
    eq_strat = (1 + weekly["basket_return"]).cumprod()
    eq_bm = (1 + weekly["bm_return"]).cumprod()
    strat_mdd = float((eq_strat / eq_strat.cummax() - 1).min())
    bm_mdd = float((eq_bm / eq_bm.cummax() - 1).min())
    years = len(weekly) / 52.0
    strat_cagr = (1 + strat_cum) ** (1 / years) - 1 if years > 0 else 0
    bm_cagr = (1 + bm_cum) ** (1 / years) - 1 if years > 0 else 0
    bm_calmar = bm_cagr / abs(bm_mdd) if bm_mdd < 0 else 0.0

    win_rate = weekly["win"].mean()
    beat_bm_rate = weekly["beat_bm"].mean()
    excess_cum = strat_cum - bm_cum

    # 출력
    print("=" * 80)
    print("  FINAL Strategy Report")
    print("=" * 80)
    print()
    print("--- 연도별 초과 수익률 (%) ---")
    print(yearly_excess.round(2).to_string())
    print()
    print("--- 리더 팩터별 누적 초과 수익률 ---")
    print(factor_excess.to_string())
    print()
    print(f"  전체 승률 (basket > 0)      : {win_rate*100:.2f}%")
    print(f"  BM 대비 승률 (basket > BM) : {beat_bm_rate*100:.2f}%")
    print(f"  전체 누적 초과 수익률      : {excess_cum*100:.2f}%")
    print(f"  전략 CAGR                  : {strat_cagr*100:.2f}%")
    print(f"  BM   Calmar                : {bm_calmar:.3f}")
    print(f"  전략 MDD                   : {strat_mdd*100:.2f}%")
    print(f"  BM   MDD                   : {bm_mdd*100:.2f}%")

    # 주별 누적 초과 수익률 선 그래프
    try:
        matplotlib.rcParams["font.family"] = "Malgun Gothic"
    except Exception:
        pass
    matplotlib.rcParams["axes.unicode_minus"] = False

    cum_excess_ts = (eq_strat - eq_bm) * 100

    fig, ax = plt.subplots(figsize=(14, 6))
    ax.plot(weekly["BaseDate"], cum_excess_ts, color="steelblue", linewidth=1.5,
            label="누적 초과 수익률 (전략 - BM)")
    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_title("주별 누적 초과 수익률 (전략 vs KOSPI)")
    ax.set_xlabel("날짜")
    ax.set_ylabel("누적 초과 수익률 (%p)")
    ax.grid(alpha=0.3)
    ax.legend()
    plt.tight_layout()

    out_path = os.path.join(OUTPUT_DIR, "FINAL_cum_excess.png")
    plt.savefig(out_path, dpi=120)
    print(f"\n  그래프 저장: {out_path}")
    plt.show()


def main() -> None:
    weekly = run_strategy()
    report(weekly)


if __name__ == "__main__":
    main()
#python factor_screening/FINAL.py