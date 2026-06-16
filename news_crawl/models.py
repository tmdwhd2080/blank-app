

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Symbol:
    code: str
    name: str = ""
    market: str = "ETF"


@dataclass(frozen=True)
class NewsItem:
    code: str
    name: str
    title: str
    description: str
    origin: str
    link: str
    published_at: datetime | None
    source: str = "naver"


@dataclass(frozen=True)
class SupplyDemand:
                                                  
    foreign_net_qty: int = 0
    institution_net_qty: int = 0
    individual_net_qty: int = 0
    foreign_net_amount: float = 0.0
    institution_net_amount: float = 0.0
    days: int = 0


@dataclass(frozen=True)
class Constituent:
    code: str
    name: str
    weight_pct: float | None = None
    change_pct: float | None = None


@dataclass(frozen=True)
class SentimentScore:
    code: str
    name: str
    s_score: float
    confidence: float
    news_count: int
    supply_demand_signal: float = 0.0
    nav_signal: float = 0.0
    summary: str = ""
    model: str = "analysis"


@dataclass(frozen=True)
class ETFFeature:
    code: str
    name: str
    market: str = "ETF"
            
    current_price: float | None = None
    today_open: float | None = None
    nav: float | None = None
    prev_nav: float | None = None
    nav_change_pct: float | None = None                           
    premium_discount_pct: float | None = None                             
    tracking_error_pct: float | None = None                
    net_assets: float | None = None                     
            
    weekly_closes: list[float] = field(default_factory=list)
    history_closes: list[float] = field(default_factory=list)
    momentum_1w: float | None = None
        
    foreign_net_qty: int = 0
    institution_net_qty: int = 0
    individual_net_qty: int = 0
    supply_demand_score: float = 0.0                               
    supply_demand_days: int = 0
          
    constituents: list[Constituent] = field(default_factory=list)
            
    s_score: float = 0.0
    s_confidence: float = 0.0
    s_model: str = "analysis"
    news_count: int = 0


@dataclass(frozen=True)
class SelectedETF:
    code: str
    name: str
    rank: int
    reason: str = ""
    score: float | None = None
    model: str = "analysis"


@dataclass(frozen=True)
class ReturnForecast:
    code: str
    name: str
    relative_return_bps: float
    confidence: float = 0.5
    reason: str = ""


@dataclass(frozen=True)
class PortfolioTarget:
    code: str
    name: str
    weight: float
    expected_return_ann: float | None = None
    current_price: float | None = None


def to_plain(obj: Any) -> Any:
    if is_dataclass(obj):
        return {k: to_plain(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {str(k): to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_plain(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj
