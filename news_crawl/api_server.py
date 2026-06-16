                       

from __future__ import annotations

import argparse
import csv
import json
import os
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from news_crawl import run_pipeline


ROOT_DIR = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT_DIR / "out" / "news_crawl"
FRONTEND_PATH = Path(__file__).resolve().parent / "dashboard.html"
USAGE_PATH = OUT_DIR / "rebalance_usage.json"
KST = ZoneInfo("Asia/Seoul")
USAGE_LOCK = threading.Lock()
PLAN_LIMITS = {
    "free": 1,
    "pro": 5,
    "premium": None,
}
ARTIFACTS = {
    "run_summary.json",
    "portfolio.csv",
    "portfolio.json",
    "features.csv",
    "features.json",
    "s_scores.csv",
    "forecasts.csv",
    "selected.json",
    "news.json",
}


@dataclass
class JobState:
    status: str = "idle"
    started_at: str | None = None
    finished_at: str | None = None
    run_id: str | None = None
    message: str = ""
    error: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "status": self.status,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "run_id": self.run_id,
                "message": self.message,
                "error": self.error,
            }

    def update(self, **kwargs: Any) -> None:
        with self.lock:
            for key, value in kwargs.items():
                setattr(self, key, value)


STATE = JobState()


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def _to_float(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return default


def _to_int(value: Any, default: int = 0) -> int:
    return int(_to_float(value, float(default)))


def _normalize_plan(value: Any) -> str:
    plan = str(value or "premium").strip().lower()
    return plan if plan in PLAN_LIMITS else "premium"


def _week_key(now: datetime | None = None) -> str:
    stamp = now.astimezone(KST) if now else datetime.now(KST)
    iso = stamp.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _usage_data() -> dict[str, Any]:
    return _read_json(USAGE_PATH, {})


def _write_usage(data: dict[str, Any]) -> None:
    USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    USAGE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _plan_status(plan: str) -> dict[str, Any]:
    plan = _normalize_plan(plan)
    limit = PLAN_LIMITS[plan]
    week = _week_key()
    usage = _usage_data()
    used = int(((usage.get(week) or {}).get(plan)) or 0)
    remaining = None if limit is None else max(0, limit - used)
    return {
        "plan": plan,
        "week": week,
        "limit": limit,
        "used": used,
        "remaining": remaining,
        "allowed": limit is None or used < limit,
    }


def _all_plan_status() -> dict[str, dict[str, Any]]:
    return {plan: _plan_status(plan) for plan in PLAN_LIMITS}


def _record_rebalance(plan: str) -> None:
    plan = _normalize_plan(plan)
    if PLAN_LIMITS[plan] is None:
        return
    week = _week_key()
    with USAGE_LOCK:
        data = _usage_data()
        week_data = data.setdefault(week, {})
        week_data[plan] = int(week_data.get(plan) or 0) + 1
        _write_usage(data)


def _display_model(value: Any) -> str:
    model = str(value or "").strip()
    if not model or "heuristic" in model.lower():
        return "analysis"
    return model


def _run_dirs() -> list[Path]:
    if not OUT_DIR.exists():
        return []
    return sorted([p for p in OUT_DIR.iterdir() if p.is_dir()], key=lambda p: p.name, reverse=True)


def _latest_run_dir() -> Path | None:
    dirs = _run_dirs()
    return dirs[0] if dirs else None


def _reference_as_of(now: datetime | None = None) -> datetime:
    now = now.astimezone(KST) if now else datetime.now(KST)
    day = now.date()
    if now.time() < time(9, 10):
        day -= timedelta(days=1)
    return datetime.combine(day, time(9, 10), tzinfo=KST)


def _parse_reference(value: str | None) -> datetime:
    if not value:
        return _reference_as_of()
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return _reference_as_of(parsed)


def _run_id_from_as_of(as_of: datetime) -> str:
    return as_of.astimezone(KST).strftime("%Y%m%d_%H%M%S")


def _reference_label(as_of: datetime) -> str:
    return as_of.astimezone(KST).strftime("%Y-%m-%d 09:10 기준 포트폴리오")


def _reference_run_dir(as_of: datetime) -> Path:
    return OUT_DIR / _run_id_from_as_of(as_of)


def _has_portfolio_rows(run_dir: Path) -> bool:
    return bool(_read_csv(run_dir / "portfolio.csv"))


def _feature_map(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {row.get("code", ""): row for row in rows if row.get("code")}


def _news_map(run_dir: Path) -> dict[str, list[dict[str, Any]]]:
    raw = _read_json(run_dir / "news.json", {})
    out: dict[str, list[dict[str, Any]]] = {}
    if isinstance(raw, dict):
        for code, items in raw.items():
            out[str(code)] = items if isinstance(items, list) else []
    return out


def _max_news_time(news: dict[str, list[dict[str, Any]]]) -> str | None:
    latest: str | None = None
    for items in news.values():
        for item in items:
            stamp = item.get("published_at")
            if stamp and (latest is None or str(stamp) > latest):
                latest = str(stamp)
    return latest


def _etf_card(
    target: dict[str, str],
    features: dict[str, dict[str, str]],
    forecasts: dict[str, dict[str, str]],
    scores: dict[str, dict[str, str]],
    selected: dict[str, dict[str, Any]],
    constituents: dict[str, list[dict[str, Any]]],
    news: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    code = target.get("code", "")
    feature = features.get(code, {})
    forecast = forecasts.get(code, {})
    score_row = scores.get(code, {})
    pick = selected.get(code, {})
    headlines = news.get(code, [])[:5]
    return {
        "code": code,
        "name": target.get("name") or feature.get("name") or code,
        "weight": _to_float(target.get("weight")),
        "price": _to_float(target.get("current_price") or feature.get("current_price")),
        "s_score": _to_float(feature.get("s_score") or score_row.get("s_score")),
        "confidence": _to_float(feature.get("s_confidence") or score_row.get("confidence")),
        "model": _display_model(feature.get("s_model") or score_row.get("model")),
        "reason": pick.get("reason") or "",
        "select_model": pick.get("model") or "",
        "rank": _to_int(pick.get("rank")),
        "relative_return_bps": _to_float(forecast.get("relative_return_bps")),
        "expected_return_ann": _to_float(target.get("expected_return_ann")),
                                
        "nav": _to_float(feature.get("nav")),
        "nav_change_pct": _to_float(feature.get("nav_change_pct")),
        "premium_discount_pct": _to_float(feature.get("premium_discount_pct")),
        "tracking_error_pct": _to_float(feature.get("tracking_error_pct")),
        "net_assets": _to_float(feature.get("net_assets")),
            
        "foreign_net_qty": _to_int(feature.get("foreign_net_qty")),
        "institution_net_qty": _to_int(feature.get("institution_net_qty")),
        "individual_net_qty": _to_int(feature.get("individual_net_qty")),
        "supply_demand_score": _to_float(feature.get("supply_demand_score")),
        "supply_demand_days": _to_int(feature.get("supply_demand_days")),
        "momentum_1w": _to_float(feature.get("momentum_1w")),
        "today_open": _to_float(feature.get("today_open")),
        "news_count": _to_int(feature.get("news_count")),
        "constituents": constituents.get(code, [])[:10],
        "headlines": headlines,
    }


def _build_dashboard_payload(run_dir: Path, reference_as_of: datetime | None = None) -> dict[str, Any]:
    reference_as_of = reference_as_of or _reference_as_of()
    summary = _read_json(run_dir / "run_summary.json", {})
    portfolio = _read_csv(run_dir / "portfolio.csv")
    features_rows = _read_csv(run_dir / "features.csv")
    score_rows = _read_csv(run_dir / "s_scores.csv")
    forecast_rows = _read_csv(run_dir / "forecasts.csv")
    selected_rows = _read_json(run_dir / "selected.json", [])
    features_json = _read_json(run_dir / "features.json", [])
    news = _news_map(run_dir)

    features = _feature_map(features_rows)
    scores = _feature_map(score_rows)
    forecasts = _feature_map(forecast_rows)
    selected = {
        str(row.get("code", "")): row
        for row in (selected_rows if isinstance(selected_rows, list) else [])
        if isinstance(row, dict)
    }
    constituents = {
        str(row.get("code", "")): (row.get("constituents") or [])
        for row in (features_json if isinstance(features_json, list) else [])
        if isinstance(row, dict)
    }
    cards = [
        _etf_card(row, features, forecasts, scores, selected, constituents, news)
        for row in portfolio
    ]
    cards.sort(key=lambda row: row["weight"], reverse=True)

    top_scores = sorted(
        [
            {
                "code": row.get("code", ""),
                "name": row.get("name", ""),
                "s_score": _to_float(row.get("s_score")),
                "confidence": _to_float(row.get("confidence")),
                "model": _display_model(row.get("model")),
                "news_count": _to_int(row.get("news_count")),
            }
            for row in score_rows
        ],
        key=lambda row: row["s_score"],
        reverse=True,
    )[:15]

    llm = summary.get("llm", {}) if isinstance(summary, dict) else {}
    return {
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "reference_as_of": reference_as_of.isoformat(),
        "reference_label": _reference_label(reference_as_of),
        "summary": summary,
        "job": STATE.snapshot(),
        "llm": llm,
        "stats": {
            "universe_size": summary.get("universe_size", len(features_rows)),
            "feature_rows": summary.get("feature_rows", len(features_rows)),
            "portfolio_count": len(cards),
            "news_symbols": sum(1 for items in news.values() if items),
            "news_items": sum(len(items) for items in news.values()),
            "max_news_time": _max_news_time(news),
        },
        "portfolio": cards,
        "top_scores": top_scores,
        "plans": _all_plan_status(),
    }


def _run_pipeline_background(payload: dict[str, Any]) -> None:
    started = datetime.now().isoformat(timespec="seconds")
    as_of = str(payload.get("as_of") or "")
    plan = _normalize_plan(payload.get("plan"))
    run_id = as_of[:19].replace("-", "").replace(":", "").replace("T", "_") if as_of else None
    STATE.update(status="running", started_at=started, finished_at=None, run_id=run_id, message="pipeline running", error=None)
    try:
        args = SimpleNamespace(
            codes=list(payload.get("codes") or []),
            max_universe=int(payload.get("max_universe") or 60),
            top_n=int(payload.get("top_n") or 10),
            capital=payload.get("capital"),
            output_dir=payload.get("output_dir"),
            as_of=payload.get("as_of"),
            plan=plan,
            risk_appetite=payload.get("risk_appetite"),
            target_return_pct=payload.get("target_return_pct"),
            use_account=bool(payload.get("use_account", True)),
            execute_orders=False,
            i_understand_real_orders=False,
            limit_orders=False,
        )
        os.chdir(ROOT_DIR)
        run_pipeline.run(args)
        _record_rebalance(plan)
        STATE.update(status="completed", finished_at=datetime.now().isoformat(timespec="seconds"), message="pipeline completed")
    except Exception as exc:                
        STATE.update(
            status="failed",
            finished_at=datetime.now().isoformat(timespec="seconds"),
            message=str(exc),
            error=traceback.format_exc(),
        )


class Handler(BaseHTTPRequestHandler):
    server_version = "NewsCrawlDashboard/0.1"

    def _send_headers(self, status: int = 200, content_type: str = "application/json; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _json(self, data: Any, status: int = 200) -> None:
        self._send_headers(status)
        self.wfile.write(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))

    def do_OPTIONS(self) -> None:              
        self._send_headers(HTTPStatus.NO_CONTENT)

    def do_GET(self) -> None:              
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/":
            self._serve_frontend()
        elif path == "/api/health":
            self._json({"ok": True, "job": STATE.snapshot()})
        elif path == "/api/runs":
            self._json({"runs": [p.name for p in _run_dirs()]})
        elif path == "/api/latest":
            query = parse_qs(parsed.query)
            reference = _parse_reference((query.get("as_of") or [None])[0])
            run_dir = _reference_run_dir(reference)
            if not run_dir.exists() or not _has_portfolio_rows(run_dir):
                self._json(
                    {
                        "error": "reference portfolio not found",
                        "reference_as_of": reference.isoformat(),
                        "reference_label": _reference_label(reference),
                        "expected_run_id": _run_id_from_as_of(reference),
                        "available_runs": [p.name for p in _run_dirs()],
                        "job": STATE.snapshot(),
                    },
                    HTTPStatus.NOT_FOUND,
                )
            else:
                self._json(_build_dashboard_payload(run_dir, reference))
        elif path.startswith("/api/runs/"):
            self._handle_run_artifact(path)
        else:
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:              
        parsed = urlparse(self.path)
        if parsed.path.rstrip("/") != "/api/run":
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._json({"error": "invalid JSON"}, HTTPStatus.BAD_REQUEST)
            return
        plan = _normalize_plan(payload.get("plan"))
        status = _plan_status(plan)
        if not status["allowed"]:
            self._json(
                {
                    "error": f"{plan.upper()} 플랜의 이번 주 리밸런싱 가능 횟수를 모두 사용했습니다.",
                    "plan_status": status,
                },
                HTTPStatus.TOO_MANY_REQUESTS,
            )
            return
        payload["plan"] = plan
        if STATE.snapshot()["status"] == "running":
            self._json({"error": "pipeline already running", "job": STATE.snapshot()}, HTTPStatus.CONFLICT)
            return
        thread = threading.Thread(target=_run_pipeline_background, args=(payload,), daemon=True)
        thread.start()
        self._json({"accepted": True, "job": STATE.snapshot(), "plan_status": status}, HTTPStatus.ACCEPTED)

    def _handle_run_artifact(self, path: str) -> None:
        parts = path.split("/")
        if len(parts) < 5:
            self._json({"error": "expected /api/runs/{run_id}/{artifact}"}, HTTPStatus.BAD_REQUEST)
            return
        run_id = parts[3]
        artifact = "/".join(parts[4:])
        if artifact not in ARTIFACTS:
            self._json({"error": "artifact not allowed"}, HTTPStatus.BAD_REQUEST)
            return
        file_path = OUT_DIR / run_id / artifact
        if not file_path.exists():
            self._json({"error": "artifact not found"}, HTTPStatus.NOT_FOUND)
            return
        if artifact.endswith(".json"):
            self._json(_read_json(file_path, {}))
        else:
            self._json({"rows": _read_csv(file_path)})

    def _serve_frontend(self) -> None:
        if not FRONTEND_PATH.exists():
            self._json({"error": "dashboard.html not found"}, HTTPStatus.NOT_FOUND)
            return
        self._send_headers(200, "text/html; charset=utf-8")
        self.wfile.write(FRONTEND_PATH.read_bytes())

    def log_message(self, fmt: str, *args: Any) -> None:
        print("[dashboard]", fmt % args)


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the news_crawl local dashboard API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    os.chdir(ROOT_DIR)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"serving news_crawl dashboard on http://{args.host}:{args.port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
