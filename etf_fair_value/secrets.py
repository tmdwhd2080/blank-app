from __future__ import annotations

import getpass
from pathlib import Path


KrxEnvPath = Path("etf_fair_value/.krx.env")


def write_krx_env(*, login_id: str | None = None, password: str | None = None) -> Path:
    login_id = login_id or input("KRX_ID: ").strip()
    password = password or getpass.getpass("KRX_PW: ")
    if not login_id:
        raise ValueError("KRX_ID is empty")
    if not password:
        raise ValueError("KRX_PW is empty")

    KrxEnvPath.parent.mkdir(parents=True, exist_ok=True)
    KrxEnvPath.write_text(
        f"KRX_ID={login_id}\nKRX_PW={password}\n",
        encoding="utf-8",
    )
    return KrxEnvPath

