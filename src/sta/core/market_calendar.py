"""Indian equity market calendar: weekends, declared holidays, close cut-off.

This module is the single source of truth for "which session just closed?".
Both the data sync and the web freshness strip import it, so they cannot
disagree about whether the cache is behind.

Holidays live in ``config/holidays.txt`` (one ISO date per line, ``#`` comments
allowed). The file is read lazily and re-read when its mtime changes, so an
exchange circular can be applied without restarting the app. A missing file
still yields weekends plus an empty holiday set, which is the old behaviour.
"""

from __future__ import annotations

import logging
from datetime import date as Date
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

## The close is published after this hour (IST).
CUTOFF_HOUR = 16
CUTOFF_MINUTE = 0

DEFAULT_HOLIDAYS = Path(__file__).resolve().parents[3] / "config" / "holidays.txt"

_cache_key: tuple[str, float, int] | None = None
_cache_value: frozenset[Date] = frozenset()


def load_holidays(path: Path | None = None) -> frozenset[Date]:
    """Parse the holiday file, cached until the file's mtime or size changes."""
    global _cache_key, _cache_value
    path = Path(path) if path else DEFAULT_HOLIDAYS
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime, stat.st_size)
    except OSError:
        return frozenset()
    if key == _cache_key:
        return _cache_value
    days: set[Date] = set()
    try:
        for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            text = raw.split("#", 1)[0].strip()
            if not text:
                continue
            try:
                days.add(datetime.strptime(text.split()[0], "%Y-%m-%d").date())
            except ValueError:
                log.warning("%s:%d: not an ISO date: %r", path, line_no, text)
    except OSError as exc:
        log.warning("could not read %s: %s", path, exc)
        return frozenset()
    _cache_key, _cache_value = key, frozenset(days)
    return _cache_value


def is_session(day: Date, holidays: frozenset[Date] | None = None) -> bool:
    """Weekday and not a declared holiday."""
    if day.weekday() >= 5:
        return False
    return day not in (holidays if holidays is not None else load_holidays())


def latest_closed_session(now: datetime | None = None,
                          holidays: frozenset[Date] | None = None) -> Date:
    """Newest session whose close is already published.

    Before the 16:00 IST cut-off the current day is not finished, so the walk
    starts at the previous day. Holidays and weekends are skipped backwards.
    A naive ``now`` is treated as IST; an aware one is converted to IST.
    """
    now = now or datetime.now(IST)
    now = now.replace(tzinfo=IST) if now.tzinfo is None else now.astimezone(IST)
    day = now.date()
    if (now.hour, now.minute) < (CUTOFF_HOUR, CUTOFF_MINUTE):
        day -= timedelta(days=1)
    holidays = load_holidays() if holidays is None else holidays
    for _ in range(30):
        if is_session(day, holidays):
            return day
        day -= timedelta(days=1)
    raise RuntimeError("no trading session found in the last 30 days")


def session_cutoff(day: Date) -> datetime:
    """The instant a session's close counts as published."""
    return datetime.combine(day, time(CUTOFF_HOUR, CUTOFF_MINUTE))


def sessions_behind(last: Date | None, expected: Date | None,
                    holidays: frozenset[Date] | None = None) -> int:
    """Number of sessions in ``(last, expected]``; 0 when not behind."""
    if last is None:
        return 10_000
    if expected is None or last >= expected:
        return 0
    holidays = load_holidays() if holidays is None else holidays
    count = 0
    day = last + timedelta(days=1)
    while day <= expected:
        if is_session(day, holidays):
            count += 1
        day += timedelta(days=1)
    return count


def sessions_between(start: Date, end: Date,
                     holidays: frozenset[Date] | None = None) -> list[Date]:
    """Every session in ``(start, end]`` in order."""
    if end <= start:
        return []
    holidays = load_holidays() if holidays is None else holidays
    out: list[Date] = []
    day = start + timedelta(days=1)
    while day <= end:
        if is_session(day, holidays):
            out.append(day)
        day += timedelta(days=1)
    return out
