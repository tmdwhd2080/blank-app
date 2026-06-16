# -*- coding: utf-8 -*-
"""개별주식선물 매수차익(cash-and-carry) 이론가·괴리·신호 계산 — 순수 로직.

이 모듈은 API에 의존하지 않는다. 입력(현물가·선물가·금리·만기·배당)만 주면
이론가/괴리/순차익/신호를 계산한다. 단독 실행하면 데모를 출력한다:

    python -m trading.arb.theory
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


# ============================================================
# 1. 잔존만기 — 주식선물 최종거래일 = 결제월의 둘째 목요일
# ============================================================
def second_thursday(year: int, month: int) -> date:
    """해당 월의 둘째 목요일(국내 주식선물 최종거래일)."""
    d = date(year, month, 1)
    # 첫 목요일까지의 오프셋 (목요일=weekday 3)
    first_thursday = 1 + (3 - d.weekday()) % 7
    return date(year, month, first_thursday + 7)


def days_to_expiry(expiry: date, today: date) -> int:
    """달력일 기준 잔존일수 (최소 0)."""
    return max((expiry - today).days, 0)


# ============================================================
# 2. 이론가 / 괴리
# ============================================================
def theoretical_price(spot: float, r: float, days: int, dividend: float = 0.0) -> float:
    """매수차익 기준 선물 이론가.

        F_이론 = S × (1 + r × T/365) − D

    Parameters
    ----------
    spot     : 현물 현재가 S
    r        : 연율 조달금리 (예: 0.035 = 3.5%).  ★ '주식 수익률'이 아니라 '현금 조달비용'
    days     : 잔존일수 T (달력일)
    dividend : 만기까지 '배당락일이 도래하는' 배당금 합 D. 배당락이 안 끼면 0.
    """
    return spot * (1.0 + r * days / 365.0) - dividend


def implied_r(spot: float, theo_price: float, days: int, dividend: float = 0.0) -> float:
    """벤더(KIS/KRX)가 준 theor_pric 에서 내재 조달금리를 역산.

        r_implied = (theor_pric + D)/S − 1) × 365/T

    KIS 이론가가 어떤 금리를 쓰는지(보통 CD91 수준) 확인용.
    """
    if days <= 0 or spot <= 0:
        return float("nan")
    return ((theo_price + dividend) / spot - 1.0) * 365.0 / days


# ============================================================
# 3. 거래비용 모델 (왕복) — 현물가 대비 비율로 환산
# ============================================================
@dataclass(frozen=True)
class CostModel:
    """왕복(진입+청산) 거래비용. 모두 '명목금액 대비 비율'.

    매수차익 1회전 구성:
      진입:  현물 매수 + 선물 매도
      청산:  현물 매도 + 선물 매수   (현물 매도에 증권거래세 발생)
    """
    commission_rate: float = 0.00015   # 편도 위탁수수료율 (현물·선물 공통 가정)
    tax_rate: float = 0.0018           # 증권거래세 — 현물 '매도(청산)' 시 1회
    slippage_rate: float = 0.0005      # 편도 호가 슬리피지 가정

    def roundtrip_rate(self) -> float:
        """왕복 총비용률.

        수수료: 현물 매수/매도 + 선물 매도/매수 = 4 × 편도수수료
        슬리피지: 4번의 체결 = 4 × 편도슬리피지
        세금: 현물 매도 1회
        """
        commission = 4 * self.commission_rate
        slippage = 4 * self.slippage_rate
        tax = self.tax_rate
        return commission + slippage + tax

    def breakdown(self) -> dict[str, float]:
        return {
            "commission": 4 * self.commission_rate,
            "slippage": 4 * self.slippage_rate,
            "tax": self.tax_rate,
            "total": self.roundtrip_rate(),
        }


# ============================================================
# 4. 종합 평가 결과
# ============================================================
@dataclass(frozen=True)
class ArbResult:
    code: str                 # 현물 종목코드
    name: str
    spot: float               # S
    futures: float            # F_market
    theo: float               # F_이론 (본인 r 기준)
    days: int                 # 잔존일수
    r: float                  # 사용한 조달금리
    dividend: float           # 사용한 배당 D
    basis: float              # F_market − S
    gap: float                # F_market − F_이론  (괴리, +면 선물 고평가)
    disparity_rate: float     # gap / F_이론       (괴리율)
    net_edge_rate: float      # 비용 차감 후 순차익률 (현물가 대비)
    cost_rate: float          # 왕복 비용률
    vendor_theo: float | None = None   # KIS가 준 이론가 (있으면)
    vendor_disparity: float | None = None
    signal: bool = False      # 매수차 신호 여부

    def as_row(self) -> dict[str, object]:
        return {
            "code": self.code,
            "name": self.name,
            "spot": self.spot,
            "futures": self.futures,
            "theo": round(self.theo, 2),
            "days": self.days,
            "r": self.r,
            "dividend": self.dividend,
            "basis": round(self.basis, 2),
            "gap": round(self.gap, 2),
            "disparity_%": round(self.disparity_rate * 100, 4),
            "net_edge_%": round(self.net_edge_rate * 100, 4),
            "cost_%": round(self.cost_rate * 100, 4),
            "vendor_theo": self.vendor_theo,
            "signal": "★BUY-ARB" if self.signal else "",
        }


def evaluate(
    *,
    code: str,
    name: str,
    spot: float,
    futures: float,
    r: float,
    days: int,
    dividend: float = 0.0,
    cost: CostModel | None = None,
    min_edge_rate: float = 0.0,
    vendor_theo: float | None = None,
    vendor_disparity: float | None = None,
) -> ArbResult:
    """한 종목 한 시점의 매수차익 평가.

    매수차 신호 = 선물이 이론가보다 '왕복 거래비용을 넘는 만큼' 고평가일 때.

        순차익률 = gap/S − 왕복비용률
        signal   = 순차익률 > min_edge_rate
    """
    cost = cost or CostModel()
    theo = theoretical_price(spot, r, days, dividend)
    basis = futures - spot
    gap = futures - theo
    disparity_rate = gap / theo if theo else float("nan")
    cost_rate = cost.roundtrip_rate()
    net_edge_rate = (gap / spot) - cost_rate if spot else float("nan")
    signal = net_edge_rate > min_edge_rate

    return ArbResult(
        code=code,
        name=name,
        spot=spot,
        futures=futures,
        theo=theo,
        days=days,
        r=r,
        dividend=dividend,
        basis=basis,
        gap=gap,
        disparity_rate=disparity_rate,
        net_edge_rate=net_edge_rate,
        cost_rate=cost_rate,
        vendor_theo=vendor_theo,
        vendor_disparity=vendor_disparity,
        signal=signal,
    )


def evaluate_vendor(
    *,
    code: str,
    name: str,
    spot: float,
    futures: float,
    vendor_theo: float,
    vendor_disparity_pct: float,
    days: int,
    cost: CostModel | None = None,
    min_edge_rate: float = 0.0,
    funding_adjust_annual: float = 0.0,
) -> ArbResult:
    """KIS 괴리율(dprt)을 '그대로 신뢰'하는 평가 (이론가 자체 계산 안 함).

    매수차 신호 = 선물이 KIS 이론가보다 고평가(dprt>0)이고, 그 폭이
    거래비용(+선택적 조달비용 보정)을 넘을 때.

        gross_edge   = dprt / 100                      (KIS 괴리율, 조달비용 이미 반영됨)
        funding_adj  = funding_adjust_annual × days/365 (본인 r ≠ KIS r 일 때만)
        net_edge     = gross_edge − funding_adj − 왕복거래비용
        signal       = net_edge > min_edge_rate

    funding_adjust_annual = (본인 조달금리 − KIS 내재금리). 0이면 'KIS 그대로 신뢰'.
    """
    cost = cost or CostModel()
    gross = vendor_disparity_pct / 100.0
    funding_adj = funding_adjust_annual * days / 365.0
    cost_rate = cost.roundtrip_rate()
    net = gross - funding_adj - cost_rate

    return ArbResult(
        code=code,
        name=name,
        spot=spot,
        futures=futures,
        theo=vendor_theo,
        days=days,
        r=funding_adjust_annual,
        dividend=0.0,
        basis=futures - spot,
        gap=futures - vendor_theo,
        disparity_rate=gross,
        net_edge_rate=net,
        cost_rate=cost_rate + funding_adj,   # 비용표시에 조달보정 포함
        vendor_theo=vendor_theo,
        vendor_disparity=vendor_disparity_pct,
        signal=net > min_edge_rate,
    )


# ============================================================
# 데모
# ============================================================
if __name__ == "__main__":
    today = date(2026, 6, 2)
    expiry = second_thursday(2026, 6)        # 2026-06-11
    T = days_to_expiry(expiry, today)

    cost = CostModel()
    print(f"만기={expiry}  잔존일수={T}")
    print(f"왕복비용 내역: {cost.breakdown()}")
    print()

    # 예시: 현물 180,000 / 선물 181,500 / 조달금리 3.5% / 배당 없음
    res = evaluate(
        code="000660", name="SK하이닉스",
        spot=180_000, futures=181_500,
        r=0.035, days=T, dividend=0.0,
        cost=cost, min_edge_rate=0.0,
    )
    for k, v in res.as_row().items():
        print(f"  {k:14}: {v}")

    print()
    # 벤더 이론가에서 내재금리 역산 예시 (KIS가 theo=180,200 을 줬다고 가정)
    r_imp = implied_r(spot=180_000, theo_price=180_200, days=T)
    print(f"내재 조달금리(역산) = {r_imp*100:.3f}%")
