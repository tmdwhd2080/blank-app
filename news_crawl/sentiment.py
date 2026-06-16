

\
\
\
\
\
   

from __future__ import annotations

from news_crawl.llm import LLMRouter, look_ahead_guard
from news_crawl.models import ETFFeature, NewsItem, SentimentScore, Symbol


POSITIVE_TERMS = (
    "상향", "호실적", "수주", "증가", "개선", "성장", "흑자", "매수",
    "순유입", "강세", "신고가", "유입", "반등", "기대",
)
NEGATIVE_TERMS = (
    "하향", "부진", "감소", "적자", "소송", "리콜", "매도", "쇼크",
    "순유출", "약세", "신저가", "유출", "급락", "우려",
)


def _news_polarity(news: list[NewsItem]) -> tuple[int, int]:
    text = " ".join(item.title + " " + item.description for item in news)
    pos = sum(text.count(term) for term in POSITIVE_TERMS)
    neg = sum(text.count(term) for term in NEGATIVE_TERMS)
    return pos, neg


def heuristic_s_score(
    news: list[NewsItem], feature: ETFFeature | None
) -> tuple[float, float, float, float, str]:
                                                                           
    pos, neg = _news_polarity(news)
    news_component = 14.0 * (pos - neg)

    nav_change = (feature.nav_change_pct if feature else None) or 0.0
    nav_signal = max(-30.0, min(30.0, nav_change * 8.0))

    sd_score = (feature.supply_demand_score if feature else 0.0) or 0.0
    supply_demand_signal = sd_score * 40.0

    raw = news_component + nav_signal + supply_demand_signal
    score = max(-100.0, min(100.0, raw))
    confidence = min(0.7, 0.2 + 0.07 * len(news) + 0.15 * abs(sd_score))
    summary = f"news(pos={pos},neg={neg}) nav%={nav_change:.2f} sd={sd_score:+.2f}"
    return score, confidence, supply_demand_signal, nav_signal, summary


def _qwen_refine(
    router: LLMRouter,
    symbol: Symbol,
    news: list[NewsItem],
    feature: ETFFeature | None,
    heuristic: tuple[float, float, float, float, str],
    *,
    as_of_label: str,
) -> tuple[float, float, str] | None:
    if not router.has_qwen:
        return None
    h_score, h_conf, sd_sig, nav_sig, _ = heuristic
    headlines = "\n".join(
        f"- ({item.published_at.isoformat() if item.published_at else '?'}) {item.title}"
        for item in news[:10]
    ) or "- (수집된 종목 뉴스 없음)"

    nav_change = (feature.nav_change_pct if feature else None)
    premium = (feature.premium_discount_pct if feature else None)
    frgn = feature.foreign_net_qty if feature else 0
    inst = feature.institution_net_qty if feature else 0
    indiv = feature.individual_net_qty if feature else 0
    momentum = (feature.momentum_1w if feature else None)

    system = (
        "너는 한국 ETF 단기 심리 점수를 매기는 퀀트 애널리스트다. "
        "주어진 휴리스틱 점수를 참고하되, 뉴스/NAV/수급을 종합해 최종 S_SCORE 를 보정한다.\n"
        + look_ahead_guard(as_of_label)
        + '반드시 JSON 한 개만 출력하라: {"s_score": <-100~100 정수>, '
        '"confidence": <0~1 실수>, "summary": "<한국어 한 줄 근거>"}'
    )
    user = (
        f"[ETF] {symbol.name} ({symbol.code})\n"
        f"[휴리스틱] s_score={h_score:.0f}, confidence={h_conf:.2f} "
        f"(수급기여={sd_sig:+.0f}, NAV기여={nav_sig:+.0f})\n"
        f"[NAV 변화율] {nav_change if nav_change is not None else 'N/A'}% / "
        f"괴리율 {premium if premium is not None else 'N/A'}%\n"
        f"[수급(최근 {feature.supply_demand_days if feature else 0}영업일 순매수 수량)] "
        f"외국인={frgn:,} 기관={inst:,} 개인={indiv:,}\n"
        f"[1주 모멘텀] {f'{momentum*100:.2f}%' if momentum is not None else 'N/A'}\n"
        f"[기준시점 이전 뉴스 헤드라인]\n{headlines}\n\n"
        "위 정보만으로 최종 S_SCORE 를 산출하라. 미래 정보를 추정하지 마라."
    )
    data = router.call_json("qwen", system, user)
    if not isinstance(data, dict):
        return None
    try:
        score = max(-100.0, min(100.0, float(data["s_score"])))
        conf = max(0.0, min(1.0, float(data.get("confidence", h_conf))))
    except (KeyError, TypeError, ValueError):
        return None
    summary = str(data.get("summary") or "qwen refined")
    return score, conf, summary


def score_etf_sentiment(
    router: LLMRouter,
    symbol: Symbol,
    news: list[NewsItem],
    feature: ETFFeature | None,
    *,
    as_of_label: str,
) -> SentimentScore:
    heuristic = heuristic_s_score(news, feature)
    h_score, h_conf, sd_sig, nav_sig, summary = heuristic

    model = "analysis"
    score, confidence = h_score, h_conf
    refined = _qwen_refine(router, symbol, news, feature, heuristic, as_of_label=as_of_label)
    if refined is not None:
        score, confidence, summary = refined
        model = router.config.qwen_model

    return SentimentScore(
        code=symbol.code,
        name=symbol.name,
        s_score=score,
        confidence=confidence,
        news_count=len(news),
        supply_demand_signal=sd_sig,
        nav_signal=nav_sig,
        summary=summary,
        model=model,
    )
