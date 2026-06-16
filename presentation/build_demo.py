# -*- coding: utf-8 -*-
"""dashboard.html → 정적 데모(docs/index.html) 생성기.

백엔드 없이 단일 파일로 동작하도록 샘플 데이터를 끼워 넣고, API 호출부를
데모용으로 치환한다. GitHub Pages 등에 올려 링크로 공유할 수 있다.

실행:  python presentation/build_demo.py
출력:  docs/index.html
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "news_crawl" / "dashboard.html"
OUT = ROOT / "docs" / "index.html"


def _etf(rank, code, name, weight, price, s_score, model, reason, nav, nav_chg,
         prem, te, net_assets, frgn, inst, indiv, sd, mom, rel_bps, news_n,
         consts, heads):
    return {
        "code": code, "name": name, "weight": weight, "price": price,
        "s_score": s_score, "confidence": round(0.45 + s_score / 400, 2),
        "model": model, "reason": reason, "select_model": "gemini", "rank": rank,
        "relative_return_bps": rel_bps, "expected_return_ann": 0.08,
        "nav": nav, "nav_change_pct": nav_chg, "premium_discount_pct": prem,
        "tracking_error_pct": te, "net_assets": net_assets,
        "foreign_net_qty": frgn, "institution_net_qty": inst, "individual_net_qty": indiv,
        "supply_demand_score": sd, "supply_demand_days": 5, "momentum_1w": mom,
        "today_open": price, "news_count": news_n,
        "constituents": [{"code": c[0], "name": c[1], "weight_pct": c[2]} for c in consts],
        "headlines": [{"published_at": h[0], "origin": h[1], "title": h[2]} for h in heads],
    }


PORTFOLIO = [
    _etf(1, "305720", "KODEX 2차전지산업", 0.21, 14820, 72, "qwen-plus",
         "외국인·기관 동반 순매수에 NAV가 5일 연속 상승, 2차전지 소재주 반등 뉴스가 우호적. 단기 모멘텀 양호.",
         14861.0, 1.84, -0.28, 0.31, 412_000_000_000, 318204, 95110, -413314, 0.64, 0.031, 142, 6,
         [("373220", "LG에너지솔루션", 24.1), ("006400", "삼성SDI", 18.7), ("247540", "에코프로비엠", 11.2),
          ("066970", "엘앤에프", 7.4), ("003670", "포스코퓨처엠", 6.9)],
         [("2026-06-13T08:42", "한국경제", "2차전지 소재주, 美 IRA 보조금 확대 기대에 강세"),
          ("2026-06-13T07:55", "이데일리", "외국인, 배터리 ETF 사흘째 순매수")]),
    _etf(2, "360750", "TIGER 미국S&P500", 0.17, 18640, 64, "qwen-plus",
         "전일 미국 증시 강세로 NAV 상승, 환헤지 미적용분 환율 우호적. 안정적 코어 자산으로 비중 배분.",
         18702.0, 0.92, -0.33, 0.12, 3_980_000_000_000, 88210, 142655, -230865, 0.41, 0.018, 96, 3,
         [("AAPL", "Apple", 7.1), ("MSFT", "Microsoft", 6.8), ("NVDA", "NVIDIA", 6.2),
          ("AMZN", "Amazon", 3.9), ("META", "Meta", 2.5)],
         [("2026-06-13T06:30", "연합뉴스", "S&P500 사상 최고치 경신… 기술주 주도")]),
    _etf(3, "091160", "KODEX 반도체", 0.15, 41250, 58, "qwen-plus",
         "HBM 수요 견조 보도와 기관 순매수 유입. 괴리율 안정적이며 추적오차 낮음.",
         41388.0, 1.21, -0.33, 0.19, 1_240_000_000_000, 201338, 178402, -379740, 0.53, 0.024, 121, 5,
         [("000660", "SK하이닉스", 22.8), ("005930", "삼성전자", 20.1), ("042700", "한미반도체", 9.3),
          ("095340", "ISC", 4.1)],
         [("2026-06-13T08:10", "매일경제", "HBM4 양산 기대에 반도체株 동반 상승"),
          ("2026-06-12T16:20", "서울경제", "외국인 반도체 ETF 순매수 전환")]),
    _etf(4, "449450", "PLUS K방산", 0.12, 26310, 55, "qwen-plus",
         "방산 수출 모멘텀 지속, 개인 매수세 강하나 외국인도 순매수. 테마 분산 차원에서 편입.",
         26298.0, 0.74, 0.05, 0.22, 286_000_000_000, 41200, 33850, -75050, 0.38, 0.046, 88, 4,
         [("012450", "한화에어로스페이스", 23.4), ("047810", "한국항공우주", 17.9),
          ("079550", "LIG넥스원", 14.2), ("064350", "현대로템", 9.1)],
         [("2026-06-13T09:01", "파이낸셜뉴스", "K방산, 중동 수출 협상 진전 소식에 강세")]),
    _etf(5, "133690", "TIGER 미국나스닥100", 0.11, 112400, 49, "qwen-plus",
         "기술주 강세 연장되나 단기 과열 신호. 코어 보완 자산으로 소폭 편입.",
         112680.0, 0.88, -0.25, 0.14, 2_150_000_000_000, 52310, 61204, -113514, 0.29, 0.012, 74, 2,
         [("NVDA", "NVIDIA", 8.9), ("AAPL", "Apple", 8.1), ("MSFT", "Microsoft", 7.7)],
         [("2026-06-13T06:45", "한국경제", "나스닥 강세 지속… AI 반도체 랠리")]),
    _etf(6, "069500", "KODEX 200", 0.10, 39120, 41, "qwen-plus",
         "지수 방어적 코어. 외국인 순매수 전환됐으나 기관 매도로 수급 혼조.",
         39208.0, 0.46, -0.22, 0.08, 6_120_000_000_000, 107355, -172021, 64666, 0.20, -0.013, 58, 1,
         [("005930", "삼성전자", 28.4), ("000660", "SK하이닉스", 8.2), ("373220", "LG에너지솔루션", 3.1)],
         []),
    _etf(7, "139660", "TIGER 200 IT", 0.08, 28760, 35, "heuristic",
         "IT 대형주 비중. 뉴스 흐름 중립, 수급 약하게 유입.",
         28812.0, 0.39, -0.18, 0.16, 410_000_000_000, 22140, 9980, -32120, 0.14, -0.004, 33, 0,
         [("005930", "삼성전자", 31.2), ("000660", "SK하이닉스", 14.4)],
         []),
    _etf(8, "091170", "KODEX 은행", 0.06, 9870, 28, "heuristic",
         "금리 환경 관망. 배당 매력 있으나 단기 모멘텀 약함. 방어적 소량 편입.",
         9884.0, 0.21, -0.14, 0.11, 320_000_000_000, 14820, 28110, -42930, 0.11, 0.002, 21, 0,
         [("105560", "KB금융", 19.8), ("055550", "신한지주", 18.1), ("086790", "하나금융지주", 16.2)],
         []),
]

DEMO_DATA = {
    "run_id": "20260613_091000",
    "run_dir": "(demo)",
    "reference_as_of": "2026-06-13T09:10:00+09:00",
    "reference_label": "2026-06-13 09:10 기준 추천 포트폴리오",
    "summary": {
        "window_start": "2026-06-12T15:30:00+09:00",
        "window_end": "2026-06-13T09:10:00+09:00",
        "mode": "etf_recommendation",
    },
    "job": {"status": "completed"},
    "llm": {"qwen": True, "gemini": True, "gpt": True},
    "stats": {
        "universe_size": 60, "feature_rows": 60, "portfolio_count": len(PORTFOLIO),
        "news_symbols": 6, "news_items": 31, "max_news_time": "2026-06-13T09:01",
    },
    "portfolio": PORTFOLIO,
    "top_scores": [
        {"code": e["code"], "name": e["name"], "s_score": e["s_score"],
         "confidence": e["confidence"], "model": e["model"], "news_count": e["news_count"]}
        for e in PORTFOLIO
    ],
}


def build() -> None:
    html = SRC.read_text(encoding="utf-8")

    # 1) 타이틀 + 데모 표식
    html = html.replace(
        "<title>ETF Alpha · AI ETF 추천 구독 서비스</title>",
        "<title>ETF Alpha · 라이브 데모</title>",
    )

    # 2) 관리자 패널/새로고침 숨김 CSS + 데모 리본
    html = html.replace(
        "    @media (max-width: 920px) {",
        "    .admin, #reload { display: none !important; }\n"
        "    .demo-ribbon { position: fixed; top: 14px; right: -38px; transform: rotate(45deg);\n"
        "      background: var(--grad); color:#fff; font-weight:800; font-size:12px; padding:6px 46px;\n"
        "      z-index: 60; box-shadow: 0 4px 14px rgba(0,0,0,.4); }\n"
        "    @media (max-width: 920px) {",
    )
    html = html.replace("<body>", '<body>\n  <div class="demo-ribbon">DEMO</div>', 1)

    # 3) 샘플 데이터 주입
    data_js = "    const DEMO_DATA = " + json.dumps(DEMO_DATA, ensure_ascii=False) + ";\n"
    html = html.replace(
        "    const FREE_VISIBLE = 2;",
        data_js + "    const FREE_VISIBLE = 2;",
    )

    # 4) loadLatest → 데모 데이터 사용 (백엔드 없음)
    old_load = """    async function loadLatest() {
      try {
        const asOf = encodeURIComponent(document.getElementById('as-of').value.trim());
        const data = await fetchJson(`/api/latest?as_of=${asOf}`);
        state.data = data;
        if (!state.selectedCode && data.portfolio?.length) state.selectedCode = data.portfolio[0].code;
        render(data);
      } catch (err) {
        setMessage(`아직 추천 결과가 없습니다 (${err.message}). 관리자 패널에서 파이프라인을 실행하세요.`, true);
        document.getElementById('window-text').textContent = '추천 데이터를 기다리는 중입니다.';
      }
    }"""
    new_load = """    function loadLatest() {
      state.data = DEMO_DATA;
      if (!state.selectedCode && DEMO_DATA.portfolio?.length) state.selectedCode = DEMO_DATA.portfolio[0].code;
      render(DEMO_DATA);
    }"""
    assert old_load in html, "loadLatest anchor not found"
    html = html.replace(old_load, new_load)

    # 5) runPipeline → 데모 안내 토스트
    old_run = """    async function runPipeline() {
      const payload = {
        as_of: document.getElementById('as-of').value.trim(),
        max_universe: Number(document.getElementById('max-universe').value || 60),
        top_n: 8
      };
      try {
        await fetchJson('/api/run', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload) });
        toast('파이프라인 실행을 시작했습니다');
        startPolling();
      } catch (err) { toast(`실행 실패: ${err.message}`); }
    }"""
    new_run = """    function runPipeline() { toast('데모 모드입니다 — 실시간 추천은 정식 서비스에서 제공됩니다'); }"""
    assert old_run in html, "runPipeline anchor not found"
    html = html.replace(old_run, new_run)

    OUT.write_text(html, encoding="utf-8")
    print(f"saved: {OUT}  ({len(html):,} bytes, {len(PORTFOLIO)} ETFs)")


if __name__ == "__main__":
    build()
