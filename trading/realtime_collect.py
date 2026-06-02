# -*- coding: utf-8 -*-
"""
실시간 시세 수집 (WebSocket)
==============================
NXT 프리마켓(08:00–08:50) 시세를 받기 위한 용도로 설계되었지만,
정규장(09:00–15:30)·시간외에도 동일하게 사용 가능.

원리:
  - REST 분봉(ka10080)은 정규장 위주라 NXT 프리마켓 봉을 보장 못 한다.
  - 대신 WebSocket '0B' (주식 체결) 실시간을 구독하면
    체결 1건마다 patch 가 푸시되고, 거래소 코드(field 290)로
    KRX/NXT 를 구분 가능.
  - 아래 스크립트는 그 패치를 받아서 그대로 CSV 에 1줄씩 append.
    분봉이 필요하면 후처리(pandas resample)로 만들면 된다.

실행 예:
    # 코스피 5종목, 8시까지 대기 후 8:50까지 수집, NXT 만 필터
    python trading/realtime_collect.py 005930 000660 035720 005380 035420 \\
        --start 08:00 --end 08:50 --exchange NXT --out out/nxt_premarket.csv

    # 즉시 시작, 30분 동안, 모든 거래소
    python trading/realtime_collect.py 005930 --duration 30
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import argparse
import asyncio
import csv
import logging
from datetime import datetime, time as dtime, timedelta

from trading.config import ConfigError, load_config
from trading.kiwoom.websocket_client import KiwoomWS


log = logging.getLogger(__name__)


# ============================================================
#  필드 매핑
#  키움 실시간 '0B' (주식체결) 의 주요 number → 의미
# ============================================================
FIELD_MAP = {
    "20":  "time",         # 체결시각 HHMMSS
    "10":  "price",        # 현재가 (체결가)
    "11":  "change",       # 전일대비
    "12":  "change_pct",   # 등락률
    "15":  "volume_signed",# 거래량 (부호 = 체결구분)
    "13":  "cum_volume",   # 누적거래량
    "14":  "cum_amount",   # 누적거래대금
    "27":  "best_ask",     # 최우선매도호가
    "28":  "best_bid",     # 최우선매수호가
    "290": "exchange",     # 거래소코드 (KRX/NXT/SOR)
}


# ============================================================
#  CSV writer
# ============================================================


class TickWriter:
    def __init__(self, path: Path | None):
        self._path = path
        self._fp = None
        self._writer = None
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fp = path.open("w", encoding="utf-8-sig", newline="")
            self._writer = csv.writer(self._fp)
            self._writer.writerow(
                ["recv_ts", "stk_cd", *FIELD_MAP.values()]
            )

    def write(self, stk_cd: str, values: dict) -> None:
        row = [datetime.now().isoformat(timespec="milliseconds"), stk_cd]
        for fid in FIELD_MAP:
            row.append(values.get(fid, ""))
        if self._writer:
            self._writer.writerow(row)
            self._fp.flush()  # 장중 중단되어도 데이터 보존
        # 콘솔에도 한 줄 — 디버깅용
        ex = values.get("290", "")
        print(f"  [{ex:>3}] {stk_cd} t={values.get('20')} "
              f"px={values.get('10')} vol={values.get('15')}")

    def close(self) -> None:
        if self._fp:
            self._fp.close()


# ============================================================
#  스케줄링 — 시작/종료 시각 대기
# ============================================================


def _parse_hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


async def _wait_until(target: datetime) -> None:
    delta = (target - datetime.now()).total_seconds()
    if delta > 0:
        log.info("starting at %s (in %.0fs)", target, delta)
        await asyncio.sleep(delta)


# ============================================================
#  메인
# ============================================================


async def run(args: argparse.Namespace) -> None:
    cfg = load_config()
    ws = KiwoomWS(cfg)

    writer = TickWriter(args.out)

    async def on_tick(packet: dict) -> None:
        stk_cd = packet.get("item", "")
        values = packet.get("values", {})
        ex = values.get("290", "")
        if args.exchange and ex and args.exchange != ex:
            return
        writer.write(stk_cd, values)

    ws.on("0B", on_tick)

    # 시작 시각 결정
    today = datetime.now().date()
    if args.start:
        start_at = datetime.combine(today, _parse_hhmm(args.start))
        if start_at < datetime.now() - timedelta(minutes=1):
            log.warning("start time %s already past, starting immediately", args.start)
            start_at = datetime.now()
    else:
        start_at = datetime.now()

    # 종료 시각 결정
    if args.end:
        end_at = datetime.combine(today, _parse_hhmm(args.end))
    elif args.duration:
        end_at = start_at + timedelta(minutes=args.duration)
    else:
        end_at = start_at + timedelta(hours=1)

    # 시작 전이면 대기
    await _wait_until(start_at)

    # WS 백그라운드 실행
    ws_task = asyncio.create_task(ws.run())
    await asyncio.sleep(1.5)  # 로그인 안정화 대기

    # 종목 등록
    log.info("subscribing %d codes (filter=%s) until %s",
             len(args.codes), args.exchange or "ALL", end_at)
    await ws.register("9001", items=args.codes, types=["0B"], refresh=True)

    # 종료까지 대기 (Ctrl+C 도 처리)
    try:
        remaining = (end_at - datetime.now()).total_seconds()
        if remaining > 0:
            await asyncio.sleep(remaining)
    except KeyboardInterrupt:
        log.info("interrupted")
    finally:
        log.info("stopping")
        ws.stop()
        try:
            await asyncio.wait_for(ws_task, timeout=3.0)
        except asyncio.TimeoutError:
            pass
        writer.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="realtime_collect",
                                description="WebSocket 실시간 시세 수집 (NXT 프리마켓 등)")
    p.add_argument("codes", nargs="+", help="종목코드 6자리")
    p.add_argument("--start", help="시작 시각 HH:MM (예: 08:00)")
    p.add_argument("--end", help="종료 시각 HH:MM (예: 08:50)")
    p.add_argument("--duration", type=int, help="시작 후 N분 (start/end 대안)")
    p.add_argument("--exchange", choices=["KRX", "NXT", "SOR"],
                   help="이 거래소 패킷만 저장 (지정 안 하면 전체)")
    p.add_argument("--out", type=Path, help="CSV 저장 경로")
    return p


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args()

    try:
        asyncio.run(run(args))
    except ConfigError as e:
        print(f"[설정 오류] {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
