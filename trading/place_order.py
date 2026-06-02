# -*- coding: utf-8 -*-
"""
주문 제출
==========
*반드시 KIWOOM_ENV=paper 에서 충분히 검증한 뒤 real 로 전환할 것.*

기본:
    # 지정가 매수: 삼성전자 1주를 70,000원에
    python -m trading.place_order buy 005930 1 70000

    # 시장가 매도: 삼성전자 1주
    python -m trading.place_order sell 005930 1 --market

    # 정정: 주문번호 0001234, 가격 71,000으로 변경 (수량 동일)
    python -m trading.place_order modify 0001234 005930 1 71000

    # 취소: 주문번호 0001234 의 잔량 전체 취소
    python -m trading.place_order cancel 0001234 005930

안전장치:
    - 실전 환경에서는 실행 전 [y/N] 확인 프롬프트
    - --yes 로 프롬프트 생략 (스크립트 자동화 용도)
    - 주문 응답(주문번호) 출력 후, 체결 상태는 account_check 으로 확인
"""

from __future__ import annotations

import sys
from pathlib import Path

# 스크립트 / 모듈 모드 모두 동작하도록 sys.path 보정
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import argparse
import logging

from trading.config import ConfigError, load_config
from trading.kiwoom import KiwoomClient
from trading.kiwoom.exceptions import KiwoomError
from trading.kiwoom.tr import order as ord_tr


# ============================================================
#  사용자 확인
# ============================================================


def confirm(prompt: str, *, force: bool) -> bool:
    """실전 운영 — 항상 yes 확인을 받는다. --yes 플래그로만 우회."""
    if force:
        return True
    print(prompt)
    ans = input("진행하시겠습니까? (yes 입력 시에만 진행): ").strip().lower()
    return ans == "yes"


# ============================================================
#  명령별 핸들러
# ============================================================


def cmd_buy(args, client: KiwoomClient) -> None:
    trde_tp = "3" if args.market else "0"
    price = 0 if args.market else args.price

    summary = (
        f"\n[매수 주문] ⚠ 실전계좌\n"
        f"  종목코드   : {args.stk_cd}\n"
        f"  수량       : {args.qty:,}주\n"
        f"  가격       : {'시장가' if args.market else f'{price:,}원'}\n"
        f"  매매구분   : {trde_tp}\n"
        f"  거래소     : {args.exchange}"
    )
    if not confirm(summary, force=args.yes):
        print("취소됨")
        return

    ack = ord_tr.buy(client, args.stk_cd, args.qty, price,
                     trde_tp=trde_tp, dmst_stex_tp=args.exchange)
    print(f"\n✓ 접수됨 — 주문번호: {ack.ord_no}")
    print("  체결 상태 확인: python -m trading.account_check")


def cmd_sell(args, client: KiwoomClient) -> None:
    trde_tp = "3" if args.market else "0"
    price = 0 if args.market else args.price

    summary = (
        f"\n[매도 주문] ⚠ 실전계좌\n"
        f"  종목코드   : {args.stk_cd}\n"
        f"  수량       : {args.qty:,}주\n"
        f"  가격       : {'시장가' if args.market else f'{price:,}원'}\n"
        f"  매매구분   : {trde_tp}\n"
        f"  거래소     : {args.exchange}"
    )
    if not confirm(summary, force=args.yes):
        print("취소됨")
        return

    ack = ord_tr.sell(client, args.stk_cd, args.qty, price,
                      trde_tp=trde_tp, dmst_stex_tp=args.exchange)
    print(f"\n✓ 접수됨 — 주문번호: {ack.ord_no}")
    print("  체결 상태 확인: python -m trading.account_check")


def cmd_modify(args, client: KiwoomClient) -> None:
    summary = (
        f"\n[정정 주문] ⚠ 실전계좌\n"
        f"  원주문번호 : {args.orig_ord_no}\n"
        f"  종목코드   : {args.stk_cd}\n"
        f"  새 수량    : {args.qty:,}주\n"
        f"  새 가격    : {args.price:,}원"
    )
    if not confirm(summary, force=args.yes):
        print("취소됨")
        return

    ack = ord_tr.modify(client, args.orig_ord_no, args.stk_cd,
                        args.qty, args.price, dmst_stex_tp=args.exchange)
    print(f"\n✓ 정정 접수 — 새 주문번호: {ack.ord_no}")


def cmd_cancel(args, client: KiwoomClient) -> None:
    summary = (
        f"\n[취소 주문] ⚠ 실전계좌\n"
        f"  원주문번호 : {args.orig_ord_no}\n"
        f"  종목코드   : {args.stk_cd}\n"
        f"  취소수량   : {'잔량 전체' if args.qty == 0 else f'{args.qty:,}주'}"
    )
    if not confirm(summary, force=args.yes):
        print("취소됨")
        return

    ack = ord_tr.cancel(client, args.orig_ord_no, args.stk_cd,
                        cancel_qty=args.qty, dmst_stex_tp=args.exchange)
    print(f"\n✓ 취소 접수 — 응답 주문번호: {ack.ord_no}")


# ============================================================
#  CLI
# ============================================================


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="place_order", description="키움 REST 주문 제출")
    p.add_argument("--exchange", default="KRX", choices=["KRX", "NXT", "SOR"],
                   help="거래소 구분 (기본 KRX)")
    p.add_argument("--yes", action="store_true",
                   help="실전 환경에서도 확인 프롬프트 생략 (자동화 용도)")

    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("buy", help="매수")
    sp.add_argument("stk_cd")
    sp.add_argument("qty", type=int)
    sp.add_argument("price", type=int, nargs="?", default=0,
                    help="지정가. --market 사용 시 생략")
    sp.add_argument("--market", action="store_true", help="시장가")

    sp = sub.add_parser("sell", help="매도")
    sp.add_argument("stk_cd")
    sp.add_argument("qty", type=int)
    sp.add_argument("price", type=int, nargs="?", default=0)
    sp.add_argument("--market", action="store_true", help="시장가")

    sp = sub.add_parser("modify", help="정정")
    sp.add_argument("orig_ord_no", help="원 주문번호")
    sp.add_argument("stk_cd")
    sp.add_argument("qty", type=int, help="정정 후 수량")
    sp.add_argument("price", type=int, help="정정 후 단가")

    sp = sub.add_parser("cancel", help="취소")
    sp.add_argument("orig_ord_no", help="원 주문번호")
    sp.add_argument("stk_cd")
    sp.add_argument("qty", type=int, nargs="?", default=0,
                    help="취소 수량 (0 또는 생략 시 잔량 전체)")

    return p


HANDLERS = {
    "buy": cmd_buy,
    "sell": cmd_sell,
    "modify": cmd_modify,
    "cancel": cmd_cancel,
}


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args()

    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"[설정 오류] {e}", file=sys.stderr)
        return 2

    client = KiwoomClient(cfg)

    if cfg.env != "real":
        print(f"[안내] KIWOOM_ENV={cfg.env} (실전 아님). real 로 사용하려면 .env 확인.")

    try:
        HANDLERS[args.cmd](args, client)
    except KiwoomError as e:
        print(f"[주문 실패] {e}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("\n중단됨")
        return 130

    return 0


if __name__ == "__main__":
    sys.exit(main())
