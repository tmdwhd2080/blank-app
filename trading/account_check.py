# -*- coding: utf-8 -*-
"""
계좌 조회
==========
.env 의 키움 계정에 연결해서 현재 계좌 상태를 한눈에 출력.

  - 환경(모의/실전)
  - 예수금 / D+2 / 주문가능금액
  - 보유종목 (종목명, 수량, 평균단가, 현재가, 평가금액, 손익률)
  - 미체결 주문
  - 합계

실행:
    python -m trading.account_check
"""

from __future__ import annotations

import sys
from pathlib import Path

# 스크립트 모드(`python trading/account_check.py`) 와
# 모듈 모드(`python -m trading.account_check`) 둘 다 동작하도록 sys.path 보정.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import logging
from datetime import datetime

from trading.config import ConfigError, load_config
from trading.kiwoom import KiwoomClient
from trading.kiwoom.exceptions import AuthError, KiwoomError
from trading.kiwoom.tr import account


# ============================================================
#  포맷 헬퍼
# ============================================================


def _won(n: int | float) -> str:
    return f"{int(n):>15,}원"


def _hr(width: int = 80) -> str:
    return "─" * width


# ============================================================
#  섹션별 출력
# ============================================================


def print_header(env: str, account_no: str) -> None:
    print(_hr())
    print(f"  계좌 조회  ({datetime.now():%Y-%m-%d %H:%M:%S})")
    print(_hr())
    print(f"  환경      : {env.upper():<10}  {'⚠ 실전계좌' if env == 'real' else '🧪 모의투자'}")
    print(f"  계좌번호  : {account_no or '(미설정)'}")
    print()


def print_deposit(client: KiwoomClient) -> int:
    print("[예수금]")
    dep = account.deposit(client)
    print(f"  예수금         {_won(dep.deposit)}")
    print(f"  D+2 추정예수금 {_won(dep.d2_estimated_deposit)}")
    print(f"  주문가능금액   {_won(dep.orderable_amount)}")
    print()
    return dep.d2_estimated_deposit


def print_holdings(client: KiwoomClient) -> int:
    print("[보유종목]")
    holdings = account.holdings(client)
    if not holdings:
        print("  (보유종목 없음)\n")
        return 0

    print(f"  {'종목':<14} {'수량':>8} {'매도가능':>8} {'평단':>10} "
          f"{'현재가':>10} {'평가금액':>14} {'손익':>12} {'수익률':>8}")
    print("  " + _hr(100))

    total_eval = 0
    total_pnl = 0
    for h in holdings:
        nm = (h.stk_nm[:13] + "…") if len(h.stk_nm) > 14 else h.stk_nm
        print(
            f"  {nm:<14} "
            f"{h.quantity:>8,} {h.available_quantity:>8,} "
            f"{float(h.avg_price):>10,.0f} "
            f"{float(h.current_price):>10,.0f} "
            f"{h.eval_amount:>14,} "
            f"{h.pnl_amount:>+12,} "
            f"{float(h.pnl_ratio):>+7.2f}%"
        )
        total_eval += h.eval_amount
        total_pnl += h.pnl_amount

    print("  " + _hr(100))
    print(f"  {'합계':<14} "
          f"{'':>8} {'':>8} {'':>10} {'':>10} "
          f"{total_eval:>14,} {total_pnl:>+12,}")
    print()
    return total_eval


def print_open_orders(client: KiwoomClient) -> None:
    print("[미체결 주문]")
    orders = account.open_orders(client)
    if not orders:
        print("  (미체결 없음)\n")
        return
    for o in orders:
        print(f"  {o}")
    print()


def print_summary(d2: int, holdings_eval: int) -> None:
    total = d2 + holdings_eval
    print("[합계]")
    print(f"  예수금(D+2)    {_won(d2)}")
    print(f"  보유주식 평가  {_won(holdings_eval)}")
    print(f"  총 평가액      {_won(total)}")
    print(_hr())


# ============================================================
#  메인
# ============================================================


def main() -> int:
    logging.basicConfig(
        level=logging.WARNING,  # 정보 출력은 print 로 직접
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"[설정 오류] {e}", file=sys.stderr)
        return 2

    client = KiwoomClient(cfg)

    # 토큰 발급 시도 — 인증 실패하면 가장 흔한 원인을 안내
    try:
        client.token  # property — 발급 트리거
    except AuthError as e:
        print(f"[인증 실패] {e}", file=sys.stderr)
        print("  → appkey/secretkey 가 올바른지 확인", file=sys.stderr)
        print(f"  → KIWOOM_ENV={cfg.env} 와 키 발급 환경(모의/실전)이 일치하는지 확인",
              file=sys.stderr)
        return 3

    print_header(cfg.env, cfg.account_no)

    try:
        d2 = print_deposit(client)
        holdings_eval = print_holdings(client)
        print_open_orders(client)
        print_summary(d2, holdings_eval)
    except KiwoomError as e:
        print(f"[조회 실패] {e}", file=sys.stderr)
        return 4

    return 0


if __name__ == "__main__":
    sys.exit(main())
# python trading/account_check.py