                       

from __future__ import annotations

from dataclasses import dataclass

from news_crawl.models import ETFFeature, PortfolioTarget, ReturnForecast


@dataclass(frozen=True)
class PortfolioResult:
    targets: list[PortfolioTarget]
    diagnostics: dict


def _returns_from_closes(closes: list[float]) -> list[float]:
    returns: list[float] = []
    for prev, cur in zip(closes, closes[1:]):
        if prev:
            returns.append(cur / prev - 1.0)
    return returns


def _cap_and_normalize(weights: list[float], max_weight: float) -> list[float]:
    n = len(weights)
    if n == 0:
        return []
    weights = [max(0.0, value) for value in weights]
    total = sum(weights)
    if total <= 0:
        weights = [1.0 / n] * n
    else:
        weights = [value / total for value in weights]

    capped = [0.0] * n
    remaining = set(range(n))
    remaining_weight = 1.0
    while remaining:
        total_raw = sum(weights[i] for i in remaining)
        if total_raw <= 0:
            equal = remaining_weight / len(remaining)
            for i in remaining:
                capped[i] = min(max_weight, equal)
            break
        changed = False
        for i in list(remaining):
            proposed = remaining_weight * weights[i] / total_raw
            if proposed > max_weight:
                capped[i] = max_weight
                remaining.remove(i)
                remaining_weight -= max_weight
                changed = True
        if not changed:
            for i in remaining:
                capped[i] = remaining_weight * weights[i] / total_raw
            break
    total = sum(capped)
    return [value / total for value in capped] if total else [1.0 / n] * n


def build_black_litterman_portfolio(
    features: list[ETFFeature],
    forecasts: list[ReturnForecast],
    *,
    market_index_closes: list[float] | None = None,
    risk_aversion: float = 2.5,
    tau: float = 0.05,
    max_weight: float = 0.20,
) -> PortfolioResult:
    try:
        import numpy as np
    except ImportError:
        equal = 1.0 / len(forecasts) if forecasts else 0.0
        targets = [
            PortfolioTarget(code=f.code, name=f.name, weight=equal, current_price=f.current_price)
            for f in features
        ]
        return PortfolioResult(targets, {"method": "equal_weight_no_numpy"})

    feature_map = {feature.code: feature for feature in features}
    forecasts = [forecast for forecast in forecasts if forecast.code in feature_map]
    if not forecasts:
        return PortfolioResult([], {"method": "empty"})

    ordered_features = [feature_map[forecast.code] for forecast in forecasts]
    min_len = min((len(feature.history_closes) for feature in ordered_features), default=0)
    if min_len < 10:
        weights = [1.0 / len(ordered_features)] * len(ordered_features)
        targets = [
            PortfolioTarget(
                code=feature.code,
                name=feature.name,
                weight=weight,
                current_price=feature.current_price,
            )
            for feature, weight in zip(ordered_features, weights)
        ]
        return PortfolioResult(targets, {"method": "equal_weight_insufficient_history"})

    returns = []
    for feature in ordered_features:
        returns.append(_returns_from_closes(feature.history_closes[-min_len:]))
    returns_matrix = np.array(returns, dtype=float).T
    cov = np.cov(returns_matrix, rowvar=False) * 252.0
    cov = np.atleast_2d(cov)
    n = cov.shape[0]
    cov = cov + np.eye(n) * 1e-6

    market_weights = np.ones(n) / n
    pi = risk_aversion * cov.dot(market_weights)

    index_returns = _returns_from_closes(market_index_closes or [])
    market_mean_ann = float(np.mean(index_returns) * 252.0) if index_returns else 0.0
    q = np.array(
        [market_mean_ann + forecast.relative_return_bps / 10_000.0 * 252.0 for forecast in forecasts],
        dtype=float,
    )
    confidence = np.array([max(0.05, min(1.0, f.confidence)) for f in forecasts], dtype=float)
    p = np.eye(n)
    tau_cov = tau * cov
    omega_diag = np.maximum(np.diag(tau_cov) * (1.0 / confidence), 1e-6)
    omega_inv = np.diag(1.0 / omega_diag)

    middle = np.linalg.inv(tau_cov) + p.T.dot(omega_inv).dot(p)
    rhs = np.linalg.inv(tau_cov).dot(pi) + p.T.dot(omega_inv).dot(q)
    posterior = np.linalg.solve(middle, rhs)

    raw = np.linalg.solve(cov + np.eye(n) * 1e-5, posterior / max(risk_aversion, 1e-6))
    weights = _cap_and_normalize(raw.tolist(), max_weight=max_weight)

    targets = [
        PortfolioTarget(
            code=feature.code,
            name=feature.name,
            weight=float(weight),
            expected_return_ann=float(exp_ret),
            current_price=feature.current_price,
        )
        for feature, weight, exp_ret in zip(ordered_features, weights, posterior.tolist())
    ]
    diagnostics = {
        "method": "black_litterman",
        "risk_aversion": risk_aversion,
        "tau": tau,
        "max_weight": max_weight,
        "market_mean_ann": market_mean_ann,
    }
    return PortfolioResult(targets, diagnostics)

