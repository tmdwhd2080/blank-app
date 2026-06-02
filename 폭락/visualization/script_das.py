import pandas as pd
import json
import warnings
warnings.filterwarnings("ignore")

# ── 파일 경로 ────────────────────────────────────────────────
BASE = "/mnt/user-data/uploads"

FILES = {
    "2026-03-03": f"{BASE}/data_2949_20260303.csv",
    "2026-03-04": f"{BASE}/data_2913_20260304.csv",
    "2026-03-05": f"{BASE}/data_4814_20260306.csv",   # 실제 3/5 데이터
    "2026-03-06": f"{BASE}/data_4018_20260306.csv",
    "2026-03-09": f"{BASE}/data_3913_20260309.csv",
}

KOSPI_IDX   = f"{BASE}/kospi.csv"
KOSDAQ_IDX  = f"{BASE}/kosdaq.csv"
KOSPI_SEC   = f"{BASE}/코스피_업종_0303.csv"
KOSDAQ_SEC  = f"{BASE}/코스닥_업종_0303.csv"

# ── 유틸 ─────────────────────────────────────────────────────
def read_csv(path):
    for enc in ["utf-8", "euc-kr", "cp949"]:
        try:
            return pd.read_csv(path, encoding=enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    raise ValueError(f"Cannot read {path}")

# ── 지수 데이터 로드 ─────────────────────────────────────────
kospi_idx = read_csv(KOSPI_IDX)
kosdaq_idx = read_csv(KOSDAQ_IDX)

kospi_idx["일자"] = kospi_idx["일자"].str.replace("/", "-")
kosdaq_idx["일자"] = kosdaq_idx["일자"].str.replace("/", "-")

kospi_ret = kospi_idx.set_index("일자")["등락률"].to_dict()
kosdaq_ret = kosdaq_idx.set_index("일자")["등락률"].to_dict()

print("=== 지수 등락률 ===")
for d in sorted(kospi_ret.keys()):
    kr = kospi_ret.get(d, "N/A")
    kdr = kosdaq_ret.get(d, "N/A")
    print(f"  {d}  KOSPI: {kr}%  KOSDAQ: {kdr}%")

# ── 업종 매핑 ────────────────────────────────────────────────
sector_kospi = read_csv(KOSPI_SEC)[["종목코드", "시장구분", "업종명", "시가총액"]]
sector_kosdaq = read_csv(KOSDAQ_SEC)[["종목코드", "시장구분", "업종명", "시가총액"]]
sector = pd.concat([sector_kospi, sector_kosdaq], ignore_index=True)

# 케이뱅크 수동 추가
kbank = pd.DataFrame([{"종목코드": "279570", "시장구분": "KOSPI", "업종명": "은행", "시가총액": 0}])
sector = pd.concat([sector, kbank], ignore_index=True)
sector["종목코드"] = sector["종목코드"].astype(str).str.zfill(6)
sector = sector.drop_duplicates("종목코드")

print(f"\n업종 매핑: KOSPI {len(sector[sector['시장구분']=='KOSPI'])}종목, KOSDAQ {len(sector[sector['시장구분']=='KOSDAQ'])}종목")

# ── 종목 데이터 로드 + 초과수익률 계산 ───────────────────────
all_results = []

for date_str, fpath in sorted(FILES.items()):
    df = read_csv(fpath)
    df["종목코드"] = df["종목코드"].astype(str).str.zfill(6)
    
    # 업종/시장 병합
    df = df.merge(sector[["종목코드", "시장구분", "업종명", "시가총액"]], on="종목코드", how="inner")
    
    # KONEX 제거
    df = df[df["시장구분"].isin(["KOSPI", "KOSDAQ"])].copy()
    
    # 시장별 벤치마크 매핑
    df["벤치마크_등락률"] = df["시장구분"].map({
        "KOSPI": kospi_ret.get(date_str, 0),
        "KOSDAQ": kosdaq_ret.get(date_str, 0),
    })
    
    df["초과수익률"] = round(df["등락률"] - df["벤치마크_등락률"], 2)
    df["날짜"] = date_str
    
    all_results.append(df)
    
    n_kospi = len(df[df["시장구분"] == "KOSPI"])
    n_kosdaq = len(df[df["시장구분"] == "KOSDAQ"])
    alpha_cnt = len(df[df["초과수익률"] > 0])
    print(f"\n{date_str}: KOSPI {n_kospi} + KOSDAQ {n_kosdaq} = {len(df)}종목, 알파 {alpha_cnt}개")

# 전체 합치기
all_df = pd.concat(all_results, ignore_index=True)

# ── Phase 1: 하락일 vs 반등일 Scatter 데이터 ─────────────────
# 폭락일: 3/3, 3/4, 3/9  /  반등일: 3/5, 3/6
drop_dates = ["2026-03-03", "2026-03-04", "2026-03-09"]
rise_dates = ["2026-03-05", "2026-03-06"]

drop_avg = all_df[all_df["날짜"].isin(drop_dates)].groupby(["종목코드", "종목명", "시장구분", "업종명"]).agg(
    하락일_평균초과수익률=("초과수익률", "mean"),
    하락일_평균등락률=("등락률", "mean"),
    하락일_총거래대금=("거래대금", "sum"),
).reset_index()

rise_avg = all_df[all_df["날짜"].isin(rise_dates)].groupby(["종목코드", "종목명", "시장구분", "업종명"]).agg(
    반등일_평균초과수익률=("초과수익률", "mean"),
    반등일_평균등락률=("등락률", "mean"),
    반등일_총거래대금=("거래대금", "sum"),
).reset_index()

scatter_df = drop_avg.merge(rise_avg, on=["종목코드", "종목명", "시장구분", "업종명"], how="inner")

# 사분면 분류
def classify_quadrant(row):
    d, r = row["하락일_평균초과수익률"], row["반등일_평균초과수익률"]
    if d > 0 and r > 0: return "Q1_방어+반등"
    elif d < 0 and r > 0: return "Q2_약세+반등"
    elif d < 0 and r < 0: return "Q3_양쪽약세"
    else: return "Q4_방어+약반등"

scatter_df["사분면"] = scatter_df.apply(classify_quadrant, axis=1)
scatter_df = scatter_df.round(2)

for q in ["Q1_방어+반등", "Q2_약세+반등", "Q3_양쪽약세", "Q4_방어+약반등"]:
    cnt = len(scatter_df[scatter_df["사분면"] == q])
    print(f"  {q}: {cnt}종목")

# ── Phase 2: 일자별 scatter 데이터 ────────────────────────────
daily_scatter = all_df[["날짜", "종목코드", "종목명", "시장구분", "업종명", "등락률", "벤치마크_등락률", "초과수익률", "거래량", "거래대금"]].copy()

# 거래량 변화율 계산
vol_by_date = all_df.pivot_table(index="종목코드", columns="날짜", values="거래량")
vol_change = vol_by_date.pct_change(axis=1) * 100
vol_change_melted = vol_change.reset_index().melt(id_vars="종목코드", var_name="날짜", value_name="거래량변화율")
daily_scatter = daily_scatter.merge(vol_change_melted, on=["종목코드", "날짜"], how="left")
daily_scatter["거래량변화율"] = daily_scatter["거래량변화율"].round(1)

# ── Phase 3: 섹터별 시계열 ────────────────────────────────────
sector_ts = all_df.groupby(["날짜", "시장구분", "업종명"]).agg(
    평균초과수익률=("초과수익률", "mean"),
    평균등락률=("등락률", "mean"),
    종목수=("종목코드", "count"),
    알파종목수=("초과수익률", lambda x: (x > 0).sum()),
    평균거래대금=("거래대금", "mean"),
).reset_index()
sector_ts = sector_ts.round(2)

# ── Phase 4: 방어력-반등력 종합 스코어 ────────────────────────
score_df = scatter_df[["종목코드", "종목명", "시장구분", "업종명", 
                        "하락일_평균초과수익률", "반등일_평균초과수익률", "사분면"]].copy()
score_df["종합스코어"] = round(score_df["하락일_평균초과수익률"] * 0.6 + score_df["반등일_평균초과수익률"] * 0.4, 2)
score_df = score_df.sort_values("종합스코어", ascending=False).reset_index(drop=True)

print("\n=== 종합스코어 TOP 20 ===")
print(score_df[["종목명", "시장구분", "업종명", "하락일_평균초과수익률", "반등일_평균초과수익률", "종합스코어", "사분면"]].head(20).to_string(index=False))

print("\n=== 종합스코어 BOTTOM 10 ===")
print(score_df[["종목명", "시장구분", "업종명", "하락일_평균초과수익률", "반등일_평균초과수익률", "종합스코어", "사분면"]].tail(10).to_string(index=False))

# ── 섹터별 종합스코어 평균 ────────────────────────────────────
sector_score = score_df.groupby(["시장구분", "업종명"]).agg(
    평균종합스코어=("종합스코어", "mean"),
    평균방어력=("하락일_평균초과수익률", "mean"),
    평균반등력=("반등일_평균초과수익률", "mean"),
    종목수=("종목코드", "count"),
    Q1비율=("사분면", lambda x: round((x == "Q1_방어+반등").sum() / len(x) * 100, 1)),
).reset_index().round(2)
sector_score = sector_score.sort_values("평균종합스코어", ascending=False)

print("\n=== 섹터별 종합스코어 (상위 15) ===")
print(sector_score.head(15).to_string(index=False))

print("\n=== 섹터별 종합스코어 (하위 10) ===")
print(sector_score.tail(10).to_string(index=False))

# ── JSON 출력 (React 대시보드용) ──────────────────────────────
output = {
    "index": {
        "dates": sorted(kospi_ret.keys()),
        "kospi": {d: kospi_ret[d] for d in sorted(kospi_ret.keys())},
        "kosdaq": {d: kosdaq_ret[d] for d in sorted(kosdaq_ret.keys())},
    },
    "scatter_drop_vs_rise": scatter_df.to_dict(orient="records"),
    "daily": daily_scatter.to_dict(orient="records"),
    "sector_timeseries": sector_ts.to_dict(orient="records"),
    "scores": score_df.to_dict(orient="records"),
    "sector_scores": sector_score.to_dict(orient="records"),
}

with open("/home/claude/analysis_data.json", "w", encoding="utf-8") as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

print(f"\n✅ JSON 저장: /home/claude/analysis_data.json")
print(f"   scatter: {len(scatter_df)}종목, daily: {len(daily_scatter)}행, sector_ts: {len(sector_ts)}행")