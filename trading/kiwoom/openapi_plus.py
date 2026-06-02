# -*- coding: utf-8 -*-
"""Kiwoom OpenAPI+ (OCX) adapter.

This module intentionally does not import PyQt5 at module import time.
The OpenAPI+ control is a Windows 32-bit ActiveX control, so importing or
creating it from a 64-bit Python process fails. Keeping imports lazy lets the
rest of the project run on normal 64-bit Python while this adapter is used only
from a dedicated OpenAPI+ runtime.
"""

from __future__ import annotations

import platform
import sys
from collections import OrderedDict
from datetime import datetime
from typing import Any, Mapping


OPENAPI_PROG_ID = "KHOPENAPI.KHOpenAPICtrl.1"


class OpenApiPlusRuntimeError(RuntimeError):
    """Raised when the local OpenAPI+ runtime is not ready."""


def _require_windows_32bit() -> None:
    if platform.system() != "Windows":
        raise OpenApiPlusRuntimeError("Kiwoom OpenAPI+ requires Windows.")
    if platform.architecture()[0] != "32bit":
        raise OpenApiPlusRuntimeError(
            "Kiwoom OpenAPI+ must be run with 32-bit Python. "
            f"Current interpreter is {platform.architecture()[0]}: {sys.executable}"
        )


def _load_qt() -> tuple[Any, Any, Any]:
    _require_windows_32bit()
    try:
        from PyQt5.QAxContainer import QAxWidget
        from PyQt5.QtCore import QEventLoop, QTimer
        from PyQt5.QtWidgets import QApplication
    except ImportError as exc:
        raise OpenApiPlusRuntimeError(
            "PyQt5 with QAxContainer is required. Install it in a 32-bit "
            "Python environment, e.g. `pip install -r trading/requirements-openapi-plus.txt`."
        ) from exc
    return QApplication, QAxWidget, (QEventLoop, QTimer)


def diagnose_environment(*, check_control: bool = False) -> list[tuple[str, bool, str]]:
    """Return environment checks without leaking user credentials."""

    rows: list[tuple[str, bool, str]] = []
    rows.append(("windows", platform.system() == "Windows", platform.system()))
    rows.append(("python_32bit", platform.architecture()[0] == "32bit", platform.architecture()[0]))
    rows.append(("python_executable", True, sys.executable))

    try:
        _load_qt()
        rows.append(("pyqt5_qaxcontainer", True, "available"))
    except Exception as exc:  # pragma: no cover - runtime diagnostic path
        rows.append(("pyqt5_qaxcontainer", False, str(exc)))
        return rows

    if check_control:
        QApplication, QAxWidget, _ = _load_qt()
        app = QApplication.instance() or QApplication(sys.argv[:1])
        ctrl = QAxWidget(OPENAPI_PROG_ID)
        ok = not ctrl.isNull()
        rows.append(("openapi_ocx", ok, OPENAPI_PROG_ID if ok else "not registered"))
        ctrl.deleteLater()
        app.processEvents()

    return rows


FUTURES_QUOTE_FIDS: "OrderedDict[str, int]" = OrderedDict(
    [
        ("trade_time", 20),
        ("last_price", 10),
        ("change", 11),
        ("change_rate", 12),
        ("best_ask", 27),
        ("best_bid", 28),
        ("volume", 15),
        ("acc_volume", 13),
        ("acc_value", 14),
        ("open", 16),
        ("high", 17),
        ("low", 18),
        ("open_interest", 195),
        ("theoretical_price", 182),
        ("theoretical_basis", 184),
        ("market_basis", 183),
        ("basis_rate", 186),
        ("basis_gap", 185),
        ("kospi200", 197),
    ]
)


FUTURES_ORDERBOOK_FIDS: "OrderedDict[str, int]" = OrderedDict(
    [
        ("quote_time", 21),
        ("best_ask", 27),
        ("best_bid", 28),
        ("ask1", 41),
        ("ask_qty1", 61),
        ("ask_count1", 101),
        ("bid1", 51),
        ("bid_qty1", 71),
        ("bid_count1", 111),
        ("ask2", 42),
        ("ask_qty2", 62),
        ("ask_count2", 102),
        ("bid2", 52),
        ("bid_qty2", 72),
        ("bid_count2", 112),
        ("ask3", 43),
        ("ask_qty3", 63),
        ("ask_count3", 103),
        ("bid3", 53),
        ("bid_qty3", 73),
        ("bid_count3", 113),
        ("ask4", 44),
        ("ask_qty4", 64),
        ("ask_count4", 104),
        ("bid4", 54),
        ("bid_qty4", 74),
        ("bid_count4", 114),
        ("ask5", 45),
        ("ask_qty5", 65),
        ("ask_count5", 105),
        ("bid5", 55),
        ("bid_qty5", 75),
        ("bid_count5", 115),
        ("total_ask_qty", 121),
        ("total_ask_count", 123),
        ("total_bid_qty", 125),
        ("total_bid_count", 127),
        ("net_bid_qty", 128),
        ("orderbook_imbalance", 137),
    ]
)


FUTURES_THEORY_FIDS: "OrderedDict[str, int]" = OrderedDict(
    [
        ("open_interest", 195),
        ("theoretical_price", 182),
        ("theoretical_basis", 184),
        ("market_basis", 183),
        ("basis_rate", 186),
        ("basis_gap", 185),
    ]
)


def futures_fids(kind: str = "all") -> "OrderedDict[str, int]":
    """Return named FIDs for KOSPI200 futures realtime collection."""

    if kind == "quote":
        return FUTURES_QUOTE_FIDS.copy()
    if kind == "orderbook":
        return FUTURES_ORDERBOOK_FIDS.copy()
    if kind == "theory":
        return FUTURES_THEORY_FIDS.copy()
    if kind != "all":
        raise ValueError("kind must be one of: quote, orderbook, theory, all")

    merged: "OrderedDict[str, int]" = OrderedDict()
    for source in (FUTURES_QUOTE_FIDS, FUTURES_ORDERBOOK_FIDS, FUTURES_THEORY_FIDS):
        for name, fid in source.items():
            merged.setdefault(name, fid)
    return merged


class OpenApiPlusClient:
    """Small blocking wrapper around Kiwoom OpenAPI+.

    It exposes only the pieces needed for file-based futures data collection:
    login, futures code listing, and realtime futures quote/orderbook capture.
    """

    def __init__(self, *, screen_no: str = "9100") -> None:
        QApplication, QAxWidget, qt_core = _load_qt()
        self._QEventLoop, self._QTimer = qt_core
        self._app = QApplication.instance() or QApplication(sys.argv[:1])
        self._ctrl = QAxWidget(OPENAPI_PROG_ID)
        if self._ctrl.isNull():
            raise OpenApiPlusRuntimeError(
                "Kiwoom OpenAPI+ OCX is not registered. Install Kiwoom OpenAPI+ "
                "and verify login in KOA Studio first."
            )

        self.screen_no = screen_no
        self._login_error: int | None = None
        self._login_loop: Any | None = None
        self._real_loop: Any | None = None
        self._active_fids: Mapping[str, int] = {}
        self._rows: list[dict[str, str]] = []
        self._tr_loop: Any | None = None
        self._tr_request: dict[str, Any] | None = None
        self._tr_rows: list[dict[str, str]] | None = None

        self._ctrl.OnEventConnect.connect(self._on_event_connect)
        self._ctrl.OnReceiveRealData.connect(self._on_receive_real_data)
        self._ctrl.OnReceiveTrData.connect(self._on_receive_tr_data)

    def login(self, *, timeout_sec: int = 120) -> None:
        """Open the Kiwoom login window and wait until login completes."""

        if self.connected:
            return

        self._login_error = None
        self._login_loop = self._QEventLoop()
        ret = self._ctrl.dynamicCall("CommConnect()")
        if int(ret) != 0:
            raise OpenApiPlusRuntimeError(f"CommConnect failed immediately: {ret}")

        self._QTimer.singleShot(timeout_sec * 1000, self._login_loop.quit)
        self._login_loop.exec_()

        if self._login_error is None:
            raise OpenApiPlusRuntimeError("OpenAPI+ login timed out.")
        if self._login_error != 0:
            raise OpenApiPlusRuntimeError(f"OpenAPI+ login failed: {self._login_error}")

    @property
    def connected(self) -> bool:
        return int(self._ctrl.dynamicCall("GetConnectState()")) == 1

    def get_login_info(self, tag: str) -> str:
        return str(self._ctrl.dynamicCall("GetLoginInfo(QString)", tag)).strip()

    def get_future_codes(self) -> list[dict[str, str]]:
        """Return index futures codes exposed by OpenAPI+."""

        raw = str(self._ctrl.dynamicCall("GetFutureList()") or "")
        codes = [code for code in raw.split(";") if code]
        rows: list[dict[str, str]] = []
        for idx, code in enumerate(codes):
            rows.append(
                {
                    "rank": str(idx),
                    "code": code,
                    "name": self.get_master_code_name(code),
                }
            )
        return rows

    def get_master_code_name(self, code: str) -> str:
        return str(self._ctrl.dynamicCall("GetMasterCodeName(QString)", code)).strip()

    def request_tr(
        self,
        *,
        rq_name: str,
        tr_code: str,
        inputs: Mapping[str, str],
        fields: list[str],
        screen_no: str | None = None,
        timeout_sec: int = 30,
    ) -> list[dict[str, str]]:
        """Request a generic OpenAPI+ TR and return selected output fields.

        Field names must match KOA Studio output names exactly. This keeps the
        wrapper useful even when Kiwoom adds or changes TRs that are not modeled
        in this repository yet.
        """

        if not fields:
            raise ValueError("fields must not be empty")
        if not self.connected:
            self.login()

        for key, value in inputs.items():
            self._ctrl.dynamicCall("SetInputValue(QString, QString)", str(key), str(value))

        self._tr_rows = None
        self._tr_request = {
            "rq_name": rq_name,
            "tr_code": tr_code,
            "fields": list(fields),
        }
        request_screen = screen_no or self.screen_no
        ret = self._ctrl.dynamicCall(
            "CommRqData(QString, QString, int, QString)",
            rq_name,
            tr_code,
            0,
            request_screen,
        )
        if int(ret) != 0:
            raise OpenApiPlusRuntimeError(f"CommRqData failed: {ret}")

        self._tr_loop = self._QEventLoop()
        self._QTimer.singleShot(timeout_sec * 1000, self._tr_loop.quit)
        self._tr_loop.exec_()

        if self._tr_rows is None:
            raise OpenApiPlusRuntimeError(f"TR request timed out: {tr_code}/{rq_name}")
        return list(self._tr_rows)

    def collect_futures_realtime(
        self,
        codes: list[str],
        *,
        seconds: int,
        fid_kind: str = "all",
    ) -> list[dict[str, str]]:
        """Collect futures realtime events for `seconds` and return flat rows."""

        if not codes:
            raise ValueError("codes must not be empty")
        if not self.connected:
            self.login()

        self._rows = []
        self._active_fids = futures_fids(fid_kind)
        fid_text = ";".join(str(fid) for fid in self._active_fids.values())
        code_text = ";".join(codes)

        ret = self._ctrl.dynamicCall(
            "SetRealReg(QString, QString, QString, QString)",
            self.screen_no,
            code_text,
            fid_text,
            "0",
        )
        if int(ret) != 0:
            raise OpenApiPlusRuntimeError(f"SetRealReg failed: {ret}")

        self._real_loop = self._QEventLoop()
        self._QTimer.singleShot(seconds * 1000, self._real_loop.quit)
        self._real_loop.exec_()
        self.disconnect_realtime()
        return list(self._rows)

    def disconnect_realtime(self) -> None:
        self._ctrl.dynamicCall("DisconnectRealData(QString)", self.screen_no)

    def _on_event_connect(self, err_code: int) -> None:
        self._login_error = int(err_code)
        if self._login_loop is not None:
            self._login_loop.quit()

    def _on_receive_real_data(self, code: str, real_type: str, _real_data: str) -> None:
        row: dict[str, str] = {
            "received_at": datetime.now().isoformat(timespec="milliseconds"),
            "code": str(code).strip(),
            "name": self.get_master_code_name(str(code).strip()),
            "real_type": str(real_type).strip(),
        }
        for name, fid in self._active_fids.items():
            value = self._ctrl.dynamicCall("GetCommRealData(QString, int)", code, fid)
            row[name] = str(value).strip()
        self._rows.append(row)

    def _on_receive_tr_data(self, *args: Any) -> None:
        if not self._tr_request:
            return

        screen_no, rq_name, tr_code, record_name, prev_next = [str(v) for v in args[:5]]
        if rq_name != self._tr_request["rq_name"]:
            return

        fields: list[str] = self._tr_request["fields"]
        repeat = int(self._ctrl.dynamicCall("GetRepeatCnt(QString, QString)", tr_code, record_name))
        count = max(repeat, 1)
        rows: list[dict[str, str]] = []
        for idx in range(count):
            row: dict[str, str] = {
                "received_at": datetime.now().isoformat(timespec="milliseconds"),
                "screen_no": screen_no,
                "rq_name": rq_name,
                "tr_code": tr_code,
                "record_name": record_name,
                "prev_next": prev_next,
                "row_index": str(idx),
            }
            for field in fields:
                value = self._ctrl.dynamicCall(
                    "GetCommData(QString, QString, int, QString)",
                    tr_code,
                    record_name,
                    idx,
                    field,
                )
                row[field] = str(value).strip()
            rows.append(row)

        self._tr_rows = rows
        if self._tr_loop is not None:
            self._tr_loop.quit()
