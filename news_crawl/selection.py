

\
\
\
\
\
   

from __future__ import annotations

from news_crawl.llm import LLMRouter, look_ahead_guard
from news_crawl.models import ETFFeature, ReturnForecast, SelectedETF


def _profile_text(investor_profile: dict | None) -> str:
    if not investor_profile:
        return ""
    risk = investor_profile.get("risk_appetite")
    target = investor_profile.get("target_return_pct")
    return f"투자자 성향: 투기적 성향 {risk}/10, 연 목표수익률 {target}%."


def _rank_key(feature: ETFFeature) -> tuple[float, float, float]:
    sentiment = feature.s_score * max(feature.s_confidence, 0.2)
    flow = (feature.supply_demand_score or 0.0) + ((feature.nav_change_pct or 0.0) / 5.0)
    momentum = feature.momentum_1w or 0.0
    return (sentiment, flow, momentum)


def prefilter_features(features: list[ETFFeature], *, limit: int) -> list[ETFFeature]:
    ranked = sorted(features, key=_rank_key, reverse=True)
    return ranked[:limit] if limit and limit > 0 else ranked


def _fallback_reason(feature: ETFFeature, *, detailed: bool) -> str:
    flow = feature.supply_demand_score or 0.0
    momentum = (feature.momentum_1w or 0.0) * 100.0
    nav = feature.nav_change_pct
    base = (
        f"S_SCORE {feature.s_score:.0f}, 수급 점수 {flow:.2f}, "
        f"1주 모멘텀 {momentum:.2f}%를 종합해 상위 후보로 분류했습니다."
    )
    if not detailed:
        return base
    nav_text = "N/A" if nav is None else f"{nav:.2f}%"
    constituents = ", ".join(c.name for c in feature.constituents[:3] if c.name) or "주요 구성종목 정보 제한"
    return (
        f"{base} NAV 변화율은 {nav_text}이고 괴리율은 "
        f"{feature.premium_discount_pct if feature.premium_discount_pct is not None else 'N/A'}%입니다. "
        f"주요 구성은 {constituents}이며, 단기 가격 흐름과 수급 방향을 함께 고려했습니다."
    )


def fallback_select(features: list[ETFFeature], *, top_n: int, detailed_reasons: bool = False) -> list[SelectedETF]:
    ranked = prefilter_features(features, limit=top_n)
    return [
        SelectedETF(
            code=f.code,
            name=f.name,
            rank=idx + 1,
            reason=_fallback_reason(f, detailed=detailed_reasons),
            score=f.s_score,
            model="analysis",
        )
        for idx, f in enumerate(ranked)
    ]


def _candidate_brief(feature: ETFFeature) -> str:
    top = ", ".join(c.name for c in feature.constituents[:3] if c.name) or "N/A"
    return (
        f"{feature.code} {feature.name} | S_SCORE={feature.s_score:.0f} "
        f"(conf {feature.s_confidence:.2f}, {feature.s_model}) | "
        f"NAV변화={feature.nav_change_pct if feature.nav_change_pct is not None else 'N/A'}% | "
        f"괴리율={feature.premium_discount_pct if feature.premium_discount_pct is not None else 'N/A'}% | "
        f"수급(외/기/개)={feature.foreign_net_qty:,}/{feature.institution_net_qty:,}/{feature.individual_net_qty:,} | "
        f"1주모멘텀={f'{feature.momentum_1w*100:.2f}%' if feature.momentum_1w is not None else 'N/A'} | "
        f"주요구성={top}"
    )


def _llm_select(
    router: LLMRouter,
    candidates: list[ETFFeature],
    *,
    top_n: int,
    as_of_label: str,
    investor_profile: dict | None = None,
    detailed_reasons: bool = False,
) -> list[SelectedETF] | None:
    if not (router.has_gemini or router.has_gpt):
        return None
    feature_map = {f.code: f for f in candidates}
    listing = "\n".join(f"- {_candidate_brief(f)}" for f in candidates)
    profile = _profile_text(investor_profile)
    reason_rule = (
        "reason은 한국어 2~3문장으로, 선택 이유와 주요 리스크를 구체적으로 써라."
        if detailed_reasons
        else "reason은 한국어 한 줄로 간결하게 써라."
    )
    reason_schema = "<한국어 2~3문장 상세 근거>" if detailed_reasons else "<한국어 한 줄 근거>"
    system = (
        "너는 한국 상장 ETF 포트폴리오를 구성하는 전문 애널리스트다. "
        "후보 ETF 들의 감성·NAV변화·수급·모멘텀·구성종목을 보고 "
        f"단기(1~5영업일) 관점에서 가장 유망한 {top_n}개를 선정한다.\n"
        + look_ahead_guard(as_of_label)
        + (profile + "\n" if profile else "")
        + "투자자 성향이 제공되면 섹터 집중도, 변동성, 기대수익의 균형을 그 성향에 맞춰 조정하라.\n"
        + "특정 섹터에 과도하게 쏠리지 않게 분산도 고려하라.\n"
        + reason_rule
        + "\n"
        '반드시 JSON 배열만 출력하라: '
        f'[{{"code":"6자리","reason":"{reason_schema}","score":<-100~100>}}]'
    )
    user = (
        f"기준 시점: {as_of_label}\n"
        + (profile + "\n" if profile else "")
        + f"아래 {len(candidates)}개 후보 중 {top_n}개를 선정하라. "
        + "제공된 정보만 사용하고 미래 데이터를 가정하지 마라.\n\n"
        + f"{listing}"
    )
    data, backend = router.screen_json(system, user)
    if not isinstance(data, list) or not data:
        return None
    selected: list[SelectedETF] = []
    seen: set[str] = set()
    for entry in data:
        if not isinstance(entry, dict):
            continue
        code = str(entry.get("code") or "").strip()
        feature = feature_map.get(code)
        if feature is None or code in seen:
            continue
        seen.add(code)
        score = entry.get("score")
        try:
            score_val = float(score) if score is not None else feature.s_score
        except (TypeError, ValueError):
            score_val = feature.s_score
        selected.append(
            SelectedETF(
                code=code,
                name=feature.name,
                rank=len(selected) + 1,
                reason=str(entry.get("reason") or "LLM 선정"),
                score=score_val,
                model=backend,
            )
        )
        if len(selected) >= top_n:
            break
    return selected or None


def select_top_etfs(
    router: LLMRouter,
    features: list[ETFFeature],
    *,
    top_n: int,
    preselect_n: int,
    as_of_label: str,
    investor_profile: dict | None = None,
    detailed_reasons: bool = False,
) -> list[SelectedETF]:
    candidates = prefilter_features(features, limit=preselect_n)
    llm = _llm_select(
        router,
        candidates,
        top_n=top_n,
        as_of_label=as_of_label,
        investor_profile=investor_profile,
        detailed_reasons=detailed_reasons,
    )
    if llm:
        return llm
    return fallback_select(candidates, top_n=top_n, detailed_reasons=detailed_reasons)


def forecast_relative_returns(
    selected: list[SelectedETF],
    features: list[ETFFeature],
    *,
    investor_profile: dict | None = None,
) -> list[ReturnForecast]:
    feature_map = {f.code: f for f in features}
    target_return_pct = float((investor_profile or {}).get("target_return_pct") or 0.0)
    target_tilt_bps = max(-30.0, min(60.0, target_return_pct * 1.5)) if investor_profile else 0.0
    forecasts: list[ReturnForecast] = []
    for etf in selected:
        feature = feature_map.get(etf.code)
        if feature is None:
            continue
        momentum_bps = (feature.momentum_1w or 0.0) * 10_000 / 5
        sentiment_bps = feature.s_score * 0.7
        flow_bps = (feature.supply_demand_score or 0.0) * 80.0
        nav_bps = (feature.nav_change_pct or 0.0) * 20.0
        pred = max(-300.0, min(300.0, momentum_bps + sentiment_bps + flow_bps + nav_bps + target_tilt_bps))
        conf = max(0.2, min(0.75, feature.s_confidence + 0.15))
        forecasts.append(
            ReturnForecast(
                code=etf.code,
                name=etf.name,
                relative_return_bps=pred,
                confidence=conf,
                reason="S_SCORE·모멘텀·수급·NAV변화 종합",
            )
        )
    return forecasts
