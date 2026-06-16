

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from news_crawl.config import StrategyConfig
from news_crawl.etf_data import (
    collect_etf_features,
    etf_universe,
    kospi_index_closes,
    symbols_from_codes,
)
from news_crawl.llm import LLMRouter
from news_crawl.models import Symbol, to_plain
from news_crawl.news_sources import NaverFinanceNewsClient, fetch_naver_finance_news_for_universe
from news_crawl.portfolio import build_black_litterman_portfolio
from news_crawl.selection import forecast_relative_returns, select_top_etfs
from news_crawl.sentiment import score_etf_sentiment
from news_crawl.utils import rebalance_news_window, write_csv, write_json
from trading.kis import KisClient, KisError


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _normalize_plan(value: object) -> str:
    plan = str(value or "premium").strip().lower()
    return plan if plan in {"free", "pro", "premium"} else "premium"


def _to_float(value: object, default: float) -> float:
    if value in (None, ""):
        return default
    try:
        return float(str(value).replace("%", "").strip())
    except ValueError:
        return default


def _investor_profile(args: argparse.Namespace, plan: str) -> dict[str, float | str]:
    if plan != "premium":
        return {}
    risk_appetite = _clamp(_to_float(getattr(args, "risk_appetite", None), 7.0), 1.0, 10.0)
    target_return_pct = _clamp(_to_float(getattr(args, "target_return_pct", None), 12.0), -20.0, 80.0)
    return {
        "risk_appetite": risk_appetite,
        "target_return_pct": target_return_pct,
        "profile_text": f"투기적 성향 {risk_appetite:.1f}/10, 연 목표수익률 {target_return_pct:.1f}%",
    }


def _apply_premium_profile(config: StrategyConfig, profile: dict[str, float | str]) -> tuple[StrategyConfig, dict[str, float]]:
    if not profile:
        return config, {}
    risk_appetite = float(profile["risk_appetite"])
    risk_ratio = risk_appetite / 10.0
    tau = _clamp(config.tau * (0.55 + 1.9 * risk_ratio), 0.02, 0.18)
    risk_aversion = _clamp(config.risk_aversion * (1.35 - 0.075 * risk_appetite), 1.0, 4.0)
    adjusted = replace(config, tau=tau, risk_aversion=risk_aversion)
    return adjusted, {
        "base_tau": config.tau,
        "tau": tau,
        "base_risk_aversion": config.risk_aversion,
        "risk_aversion": risk_aversion,
    }


def _safe_kis_client() -> KisClient | None:
    try:
        return KisClient()
    except KisError:
        return None


def _load_universe(*, codes: list[str], max_universe: int) -> list[Symbol]:
    if codes:
        return symbols_from_codes(codes)
    return etf_universe(limit=max_universe)


def _run_dir(config: StrategyConfig, now: datetime) -> Path:
    path = config.output_dir / now.strftime("%Y%m%d_%H%M%S")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _parse_as_of(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def run(args: argparse.Namespace) -> int:
    config = StrategyConfig.from_env()
    plan = _normalize_plan(getattr(args, "plan", None))
    investor_profile = _investor_profile(args, plan)
    if args.max_universe is not None:
        config = replace(config, max_universe=args.max_universe)
    if args.top_n is not None:
        config = replace(config, top_n=args.top_n)
    if args.output_dir:
        config = replace(config, output_dir=Path(args.output_dir))
    config, premium_params = _apply_premium_profile(config, investor_profile)

    as_of = _parse_as_of(args.as_of)
    now = as_of or datetime.now()
    window_start, window_end = rebalance_news_window(
        now=as_of,
        timezone=config.timezone,
        start_hhmm=config.news_window_start,
        end_hhmm=config.news_window_end,
    )
    out_dir = _run_dir(config, now)
    as_of_label = (as_of or datetime.now()).strftime("%Y-%m-%d %H:%M KST")

    print(f"[window] {window_start.isoformat()} -> {window_end.isoformat()}")
    print(f"[mode] ETF recommendation (dry-run, plan={plan})")
    print(f"[out] {out_dir}")

    kis_client = _safe_kis_client()
    if kis_client is None:
        raise SystemExit("KIS client is required for ETF data. Set KIS_APP_KEY/KIS_APP_SECRET.")

    router = LLMRouter(config)
    print(
        f"[llm] qwen={'on' if router.has_qwen else 'off'} "
        f"gemini={'on' if router.has_gemini else 'off'} "
        f"gpt={'on' if router.has_gpt else 'off'} (off=휴리스틱 폴백)"
    )

    naver = NaverFinanceNewsClient()

    symbols = _load_universe(codes=args.codes, max_universe=config.max_universe)
    print(f"[universe] {len(symbols)} ETFs")
    write_json(out_dir / "universe.json", to_plain(symbols))

    news_by_code = fetch_naver_finance_news_for_universe(
        naver,
        symbols,
        window_start=window_start,
        window_end=window_end,
        max_items=config.max_news_per_symbol,
    )
    write_json(out_dir / "news.json", to_plain(news_by_code))
    print("[news] collected")

                                      
    features = collect_etf_features(
        kis_client,
        symbols,
        scores={},
        history_days=config.history_days,
        supply_demand_days=config.supply_demand_days,
        as_of=as_of,
    )
    feature_map = {f.code: f for f in features}
    print(f"[features] {len(features)} ETFs (NAV·수급·모멘텀)")

                               
    scores = {}
    for symbol in symbols:
        scores[symbol.code] = score_etf_sentiment(
            router,
            symbol,
            news_by_code.get(symbol.code, []),
            feature_map.get(symbol.code),
            as_of_label=as_of_label,
        )
    write_json(out_dir / "s_scores.json", to_plain(scores))
    write_csv(out_dir / "s_scores.csv", [to_plain(s) for s in scores.values()])
    print("[s_score] QWEN/heuristic done")

                      
    features = [
        replace(
            f,
            s_score=scores[f.code].s_score,
            s_confidence=scores[f.code].confidence,
            s_model=scores[f.code].model,
            news_count=scores[f.code].news_count,
        )
        for f in features
    ]
    write_json(out_dir / "features.json", to_plain(features))
    write_csv(
        out_dir / "features.csv",
        [
            {k: v for k, v in to_plain(f).items() if k not in ("constituents", "history_closes", "weekly_closes")}
            for f in features
        ],
    )

                    
    selected = select_top_etfs(
        router,
        features,
        top_n=config.top_n,
        preselect_n=config.preselect_n,
        as_of_label=as_of_label,
        investor_profile=investor_profile,
        detailed_reasons=plan == "premium",
    )
    write_json(out_dir / "selected.json", to_plain(selected))
    print("[selection] " + ", ".join(etf.code for etf in selected))

    forecasts = forecast_relative_returns(selected, features, investor_profile=investor_profile)
    write_json(out_dir / "forecasts.json", to_plain(forecasts))
    write_csv(out_dir / "forecasts.csv", [to_plain(f) for f in forecasts])

    selected_codes = {etf.code for etf in selected}
    selected_features = [f for f in features if f.code in selected_codes]
    index_closes = kospi_index_closes(kis_client, history_days=config.history_days, as_of=as_of)
    portfolio = build_black_litterman_portfolio(
        selected_features,
        forecasts,
        market_index_closes=index_closes,
        risk_aversion=config.risk_aversion,
        tau=config.tau,
        max_weight=config.max_weight,
    )
    diagnostics = dict(portfolio.diagnostics)
    diagnostics["plan"] = plan
    if investor_profile:
        diagnostics["investor_profile"] = investor_profile
        diagnostics["premium_parameters"] = premium_params
    write_json(
        out_dir / "portfolio.json",
        {"targets": to_plain(portfolio.targets), "diagnostics": diagnostics},
    )
    write_csv(out_dir / "portfolio.csv", [to_plain(t) for t in portfolio.targets])
    print("[portfolio] built")

    write_json(
        out_dir / "run_summary.json",
        {
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "as_of": as_of_label,
            "mode": "etf_recommendation",
            "plan": plan,
            "investor_profile": investor_profile,
            "premium_parameters": premium_params,
            "universe_size": len(symbols),
            "feature_rows": len(features),
            "selected": [etf.code for etf in selected],
            "llm": {
                "qwen": router.has_qwen,
                "gemini": router.has_gemini,
                "gpt": router.has_gpt,
            },
            "out_dir": str(out_dir),
        },
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the ETF recommendation pipeline (KIS + LLM).")
    parser.add_argument("--codes", nargs="*", default=[], help="Optional explicit ETF codes.")
    parser.add_argument("--max-universe", type=int, help="Maximum ETFs to scan.")
    parser.add_argument("--top-n", type=int, help="Number of final recommended ETFs.")
    parser.add_argument("--output-dir", help="Output directory for run artifacts.")
    parser.add_argument("--plan", default="premium", choices=["free", "pro", "premium"], help="Subscription plan.")
    parser.add_argument("--risk-appetite", type=float, help="Premium risk appetite from 1 to 10.")
    parser.add_argument("--target-return-pct", type=float, help="Premium annual target return in percent.")
    parser.add_argument(
        "--as-of",
        help="Assumed run time in ISO format, e.g. 2026-06-13T09:10:00+09:00.",
    )
    return parser


def main() -> int:
    return run(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
