                       

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_local_env() -> None:
    package_dir = Path(__file__).resolve().parent
    root_dir = package_dir.parent
    for path in (
        root_dir / ".env",
        package_dir / ".env",
        package_dir / ".env.local",
    ):
        _load_env_file(path)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class StrategyConfig:
    timezone: str = "Asia/Seoul"
    news_window_start: str = "15:30"
    news_window_end: str = "09:10"
    rebalance_time: str = "09:15"
    lookback_days: int = 7
    history_days: int = 90
    top_n: int = 8
    preselect_n: int = 40
    max_universe: int = 60
    max_news_per_symbol: int = 8
    supply_demand_days: int = 5
    starting_capital: int = 10_000_000
    max_weight: float = 0.25
    risk_aversion: float = 2.5
    tau: float = 0.05
    dry_run: bool = True
    output_dir: Path = Path("out") / "news_crawl"
                                                                    
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_model: str = "qwen-plus"
    qwen_api_key: str = ""
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    gemini_model: str = "gemini-2.5-flash"
    gemini_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    openai_api_key: str = ""

    @classmethod
    def from_env(cls) -> "StrategyConfig":
        load_local_env()
        return cls(
            timezone=os.environ.get("NEWS_CRAWL_TIMEZONE", cls.timezone),
            news_window_start=os.environ.get("NEWS_WINDOW_START", cls.news_window_start),
            news_window_end=os.environ.get("NEWS_WINDOW_END", cls.news_window_end),
            rebalance_time=os.environ.get("REBALANCE_TIME", cls.rebalance_time),
            lookback_days=_env_int("NEWS_LOOKBACK_DAYS", cls.lookback_days),
            history_days=_env_int("BL_HISTORY_DAYS", cls.history_days),
            top_n=_env_int("TOP_N", cls.top_n),
            preselect_n=_env_int("PRESELECT_N", cls.preselect_n),
            max_universe=_env_int("MAX_UNIVERSE", cls.max_universe),
            max_news_per_symbol=_env_int("MAX_NEWS_PER_SYMBOL", cls.max_news_per_symbol),
            supply_demand_days=_env_int("SUPPLY_DEMAND_DAYS", cls.supply_demand_days),
            starting_capital=_env_int("STARTING_CAPITAL", cls.starting_capital),
            max_weight=_env_float("MAX_WEIGHT", cls.max_weight),
            risk_aversion=_env_float("RISK_AVERSION", cls.risk_aversion),
            tau=_env_float("BL_TAU", cls.tau),
            dry_run=_env_bool("DRY_RUN", cls.dry_run),
            output_dir=Path(os.environ.get("NEWS_CRAWL_OUTPUT_DIR", str(cls.output_dir))),
            qwen_base_url=os.environ.get("QWEN_BASE_URL", cls.qwen_base_url),
            qwen_model=os.environ.get("QWEN_MODEL", cls.qwen_model),
            qwen_api_key=os.environ.get("QWEN_API_KEY", os.environ.get("DASHSCOPE_API_KEY", cls.qwen_api_key)),
            gemini_base_url=os.environ.get("GEMINI_BASE_URL", cls.gemini_base_url),
            gemini_model=os.environ.get("GEMINI_MODEL", cls.gemini_model),
            gemini_api_key=os.environ.get("GEMINI_API_KEY", os.environ.get("GOOGLE_API_KEY", cls.gemini_api_key)),
            openai_base_url=os.environ.get("OPENAI_BASE_URL", cls.openai_base_url),
            openai_model=os.environ.get("OPENAI_MODEL", cls.openai_model),
            openai_api_key=os.environ.get("OPENAI_API_KEY", cls.openai_api_key),
        )

