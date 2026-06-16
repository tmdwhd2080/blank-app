                       

from __future__ import annotations

import csv
import html
import json
import re
from datetime import date, datetime, time, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(value: str | None) -> str:
    text = html.unescape(value or "")
    return _TAG_RE.sub("", text).strip()


def parse_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    if text.startswith("+"):
        text = text[1:]
    try:
        return float(text)
    except ValueError:
        return None


def parse_int(value: Any) -> int | None:
    number = parse_float(value)
    return int(number) if number is not None else None


def parse_pubdate(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


def previous_business_day(day: date) -> date:
    cur = day - timedelta(days=1)
    while cur.weekday() >= 5:
        cur -= timedelta(days=1)
    return cur


def next_business_day(day: date) -> date:
    cur = day + timedelta(days=1)
    while cur.weekday() >= 5:
        cur += timedelta(days=1)
    return cur


def parse_hhmm(value: str) -> time:
    hh, mm = value.split(":", 1)
    return time(int(hh), int(mm))


def rebalance_news_window(
    *,
    now: datetime | None = None,
    timezone: str = "Asia/Seoul",
    start_hhmm: str = "15:30",
    end_hhmm: str = "09:10",
) -> tuple[datetime, datetime]:
    tz = ZoneInfo(timezone)
    now = now.astimezone(tz) if now else datetime.now(tz)
    start_clock = parse_hhmm(start_hhmm)
    end_clock = parse_hhmm(end_hhmm)

    if now.time() < start_clock:
        start_day = previous_business_day(now.date())
        end_day = now.date()
    else:
        start_day = now.date()
        end_day = next_business_day(now.date())

    return (
        datetime.combine(start_day, start_clock, tzinfo=tz),
        datetime.combine(end_day, end_clock, tzinfo=tz),
    )


def extract_json(text: str) -> Any:
    text = text.strip()
    if not text:
        raise ValueError("empty JSON response")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    starts = [i for i, ch in enumerate(text) if ch in "[{"]
    for start in starts:
        stack: list[str] = []
        in_string = False
        escape = False
        for idx in range(start, len(text)):
            ch = text[idx]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch in "[{":
                stack.append("]" if ch == "[" else "}")
            elif ch in "]}":
                if not stack or ch != stack[-1]:
                    break
                stack.pop()
                if not stack:
                    return json.loads(text[start : idx + 1])
    raise ValueError("could not find JSON object in response")


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        keys: list[str] = []
        for row in rows:
            for key in row:
                if key not in keys:
                    keys.append(key)
        fieldnames = keys
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

