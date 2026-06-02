# -*- coding: utf-8 -*-
"""
Default launcher for the ETF iNAV/LOB alpha collector.

Run:
    python run.py

By default this waits for 08:55, collects until 15:30, writes CSV under out/,
and prints BUY ALERT lines while continuing to run.

To add more ETFs, edit DEFAULT_CODES below or pass CLI arguments:
    python run.py --code 457990 --code 069500 --end 15:30
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from trading.etf_inav_lob_alpha_collect import main


DEFAULT_CODES = ("457990", "0035T0", "364690")
DEFAULT_START = "08:55"
DEFAULT_END = "15:30"


def _default_argv() -> list[str]:
    stamp = datetime.now().strftime("%Y%m%d")
    code_part = "_".join(DEFAULT_CODES)
    out = Path("out") / f"etf_inav_lob_alpha_{code_part}_{stamp}.csv"

    args: list[str] = []
    for code in DEFAULT_CODES:
        args.extend(["--code", code])

    args.extend(
        [
            "--start",
            DEFAULT_START,
            "--end",
            DEFAULT_END,
            "--out",
            str(out),
            "--alert-cooldown-sec",
            "30",
        ]
    )
    return [sys.argv[0], *args]


if __name__ == "__main__":
    if len(sys.argv) == 1:
        sys.argv = _default_argv()
    raise SystemExit(main())
# python run.py