"""View models for the server-rendered pages.

Every page gets a plain dict; templates stay logic-free. All rupee amounts are
formatted with Indian digit grouping (lakh/crore), not Western thousands.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date as Date
from datetime import datetime
from pathlib import Path

from ..core.config import Settings
from ..core.market_calendar import latest_closed_session, sessions_behind
from ..data.sync import attempted_since, last_stored_date
from ..live.db import Database
from ..live.settings import load_live_settings
from ..live.signals import latest_session

REASON_LABELS = {
    "stop_initial": "Initial stop",
    "stop_trailing": "Trailing stop",
    "stop_profit_lock": "Profit lock",
    "stop_failed_breakout": "Failed breakout",
    "stop_atr_collapsed": "Volatility collapse",
    "target": "Target",
    "time": "Time stop",
    "trend": "Trend exit",
    "end": "Open at end",
}

STATUS_LABELS = {
    "new": "Not tracked",
    "track": "Will fill next open",
    "skip": "Skipped",
    "suggested": "Queued for next open",
    "open": "Open",
    "closed": "Closed",
    "expired": "Expired",
    "cancelled": "Cancelled",
}


# --------------------------------------------------------------- formatting


def inr(value, decimals: int = 0) -> str:
    if value is None or value == "":
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    negative = number < 0
    text = f"{abs(number):,.{decimals}f}"
    integer, _, fraction = text.partition(".")
    digits = integer.replace(",", "")
    if len(digits) > 3:
        last3, rest = digits[-3:], digits[:-3]
        groups: list[str] = []
        while len(rest) > 2:
            groups.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            groups.insert(0, rest)
        integer = ",".join(groups) + "," + last3
    out = "₹" + integer + (f".{fraction}" if fraction else "")
    return "-" + out if negative else out


def pct(value, decimals: int = 1, signed: bool = True) -> str:
    if value is None:
        return "—"
    try:
        number = float(value) * 100
    except (TypeError, ValueError):
        return "—"
    sign = "+" if signed and number > 0 else ""
    return f"{sign}{number:,.{decimals}f}%"


def day_label(value) -> str:
    if not value:
        return "—"
    text = str(value)[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").strftime("%d %b %Y")
    except ValueError:
        return text


def short_day(value) -> str:
    if not value:
        return "—"
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").strftime("%d %b")
    except ValueError:
        return str(value)[:10]


def tone(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    return "pos" if number > 0 else "neg" if number < 0 else ""


def reason_label(value) -> str:
    if not value:
        return "—"
    return REASON_LABELS.get(str(value), str(value).replace("_", " ").title())


# ------------------------------------------------------------------ freshness

def _latest_business_day(today: Date, now: datetime | None = None) -> Date:
    """Last session whose close is already published (16:00 IST cut-off)."""
    now = now or datetime.now()
    return latest_closed_session(datetime.combine(today, now.time()))


def _stamp_label(value) -> str | None:
    if not value:
        return None
    text = str(value).replace("T", " ")
    try:
        return datetime.strptime(text[:16], "%Y-%m-%d %H:%M").strftime("%d %b, %H:%M")
    except ValueError:
        return text


def freshness(settings: Settings, db: Database) -> dict:
    """Is the cached history current enough to trust a signal?

    ``source_lag`` is the honest third state: the cache is behind the exchange
    calendar, but a successful sync already ran after that close and upstream
    still has no newer bar. It is shown as amber, never as "stale", and it does
    not trigger another catch-up sync.
    """
    session = latest_session(settings, db)
    expected = latest_closed_session()
    behind = sessions_behind(session, expected)
    last_sync = db.last_sync_log()
    finished = db.last_finished_sync_log()
    coverage = db.get_json("sync.coverage", {}) or {}
    universe_day = db.universe_day()
    files = len(list(settings.daily_dir.glob("*.parquet"))) if settings.daily_dir.exists() else 0
    status = "missing" if session is None else ("fresh" if behind <= 0 else "stale")
    # a session only a minority reached is not the session (the sync enforces
    # the same 50% floor before writing coverage)
    if (status == "fresh" and coverage.get("day") == expected.isoformat()
            and coverage.get("pct", 100) < 50):
        status = "stale"
    source_lag = False
    if status == "stale" and behind > 0 and attempted_since(db, expected):
        status = "source_lag"
        source_lag = True
    return {
        "status": status,
        "session": session,
        "session_label": day_label(session),
        "expected": expected,
        "expected_label": day_label(expected),
        "behind": max(0, behind),
        "symbols": files,
        "coverage": coverage,
        "universe_day": universe_day,
        "newer_symbols": db.symbols_at(expected) if session else 0,
        "source_lag": source_lag,
        "syncing": bool(last_sync and last_sync["finished_at"] is None),
        "last_sync_at": (finished["finished_at"] if finished else None),
        "last_sync_label": _stamp_label(finished["finished_at"]) if finished else None,
        "last_sync_status": (finished["status"] if finished else None),
    }


# ---------------------------------------------------------------------- today

SIGNAL_STATE = {
    "suggested": "Queued for next open",
    "open": "Open",
    "closed": "Closed",
    "expired": "No slot",
    "cancelled": "Skipped",
    "missed": "Not taken",
}


def signal_view(db: Database, row) -> dict:
    """One suggestion plus the state of the trade it produced.

    A suggestion is only actionable on the session it was generated from: the
    entry window is the next open, so yesterday's list cannot be entered now.
    """
    trade = db.trade_for_signal(row["id"])
    actionable = str(row["signal_date"])[:10] == str(db.trusted_day() or "")
    status = trade["status"] if trade else (None if actionable else "missed")
    return {
        "id": row["id"], "symbol": row["symbol"], "name": row["name"],
        "cap_class": row["cap_class"], "score": row["score"],
        "close": row["close"], "entry_plan": row["entry_plan"],
        "stop_initial": row["stop_initial"], "qty": row["qty"],
        "risk": row["risk"], "dist_high": row["dist_high"],
        "action": row["action"], "trade_status": status,
        "status_label": SIGNAL_STATE.get(status, ""),
        "actionable": actionable,
        "can_track": actionable and (trade is None or status == "cancelled")
                     and row["action"] in ("new", "skip", "track"),
    }


def today_view(settings: Settings, db: Database, job: dict | None = None) -> dict:
    live = load_live_settings(settings, db)
    signal_day = db.latest_signal_day() or latest_session(settings, db)
    raw_signals = db.signals_for(signal_day) if signal_day else []
    signals = [signal_view(db, row) for row in raw_signals]
    equity = db.equity_series()
    nav = equity[-1]["nav"] if equity else live.position_size * live.max_positions
    prev_nav = equity[-2]["nav"] if len(equity) > 1 else None
    open_trades = db.open_trades()
    queued = db.suggested_trades()
    signal_day_str = str(signal_day) if signal_day else None
    recent_exits = []
    for row in db.closed_trades():
        if signal_day_str and str(row["exit_date"])[:10] >= signal_day_str:
            shares = int(row["shares"] or 0)
            entry = float(row["entry_price"] or 0)
            recent_exits.append({
                "symbol": row["symbol"],
                "exit_date": row["exit_date"],
                "exit_date_label": short_day(row["exit_date"]),
                "exit_price": row["exit_price"],
                "exit_reason": reason_label(row["exit_reason"]),
                "reason_key": row["exit_reason"],
                "pnl": row["pnl"],
                "return_pct": ((float(row["pnl"] or 0) / (entry * shares))
                               if entry and shares else None),
                "days_held": row["days_held"],
            })
    recent_exits.sort(key=lambda r: str(r["exit_date"]), reverse=True)
    return {
        "page": "today",
        "freshness": freshness(settings, db),
        "signal_day": signal_day,
        "signal_day_label": day_label(signal_day),
        "signals": signals,
        "nav": nav,
        "nav_delta": (nav - prev_nav) if prev_nav else None,
        "nav_delta_pct": (nav / prev_nav - 1.0) if prev_nav else None,
        "slots_used": len(open_trades),
        "slots_max": live.max_positions,
        "queued": [
            {"symbol": r["symbol"], "status": r["status"]} for r in queued
        ],
        "recent_exits": recent_exits,
        "free_slots": max(0, live.max_positions - len(open_trades)),
        "position_size": live.position_size,
        "auto_track": live.auto_track,
        "job": job or {},
    }


# -------------------------------------------------------------------- tracker

def tracker_view(settings: Settings, db: Database, *,
                 year: str | None = None, reason: str | None = None,
                 result: str | None = None, job: dict | None = None) -> dict:
    marks = db.latest_marks()
    state = freshness(settings, db)
    session_day = str(state["session"])[:10] if state["session"] else None
    open_rows = []
    for row in db.open_trades():
        mark = marks.get(row["id"])
        entry = float(row["entry_price"] or 0)
        marked = bool(mark and mark["close"] is not None)
        last = float(mark["close"]) if marked else None
        mark_date = str(mark["date"])[:10] if marked else None
        open_rows.append({
            "id": row["id"], "symbol": row["symbol"],
            "entry_date": row["entry_date"], "entry_date_label": short_day(row["entry_date"]),
            "entry_price": entry, "shares": row["shares"],
            "marked": marked,
            "last": last,
            "mark_date": mark_date,
            "mark_date_label": short_day(mark_date) if mark_date else None,
            "stale": bool(mark_date and session_day and mark_date < session_day),
            "unrealized": ((last - entry) * int(row["shares"] or 0)
                           if marked else None),
            "unrealized_pct": ((last / entry - 1.0) if marked and entry else None),
            "stop_live": row["stop_live"], "high_water": row["high_water"],
            "days_held": row["days_held"],
            "distance_to_stop": ((last / float(row["stop_live"]) - 1.0)
                                 if marked and row["stop_live"] else None),
        })
    open_rows.sort(key=lambda r: str(r["entry_date"] or ""), reverse=True)
    stale_marks = sum(1 for r in open_rows if r["stale"])

    closed = [dict(r) for r in db.closed_trades()]
    years = sorted({str(r["exit_date"])[:4] for r in closed if r["exit_date"]},
                   reverse=True)
    reasons = sorted({r["exit_reason"] for r in closed if r["exit_reason"]})
    filtered = [
        r for r in closed
        if (not year or str(r["exit_date"])[:4] == year)
        and (not reason or r["exit_reason"] == reason)
        and (not result
             or (result == "win" and (r["pnl"] or 0) > 0)
             or (result == "loss" and (r["pnl"] or 0) <= 0))
    ]
    filtered.sort(key=lambda r: str(r["exit_date"] or ""), reverse=True)
    for row in filtered:
        row["exit_date_label"] = short_day(row["exit_date"])
        row["return_pct"] = (row["pnl"] or 0) / max(
            (row["entry_price"] or 0) * (row["shares"] or 0), 1e-9)
        row["reason_label"] = reason_label(row["exit_reason"])
    return {
        "page": "tracker",
        "freshness": state,
        "open_rows": open_rows,
        "stale_marks": stale_marks,
        "closed": filtered,
        "stats": trade_stats(filtered),
        "years": years, "reasons": reasons,
        "filters": {"year": year or "", "reason": reason or "",
                    "result": result or ""},
        "reason_labels": REASON_LABELS,
        "slots_max": load_live_settings(settings, db).max_positions,
        "job": job or {},
    }


def trade_stats(rows: list[dict]) -> dict:
    closed = [r for r in rows if r.get("status") == "closed"]
    if not closed:
        return {"trades": 0, "win_rate": None, "profit_factor": None,
                "expectancy": None, "avg_win": None, "avg_loss": None,
                "avg_hold": None, "total_pnl": 0.0, "best": None, "worst": None}
    pnls = [float(r["pnl"] or 0) for r in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    holds = [int(r["days_held"] or 0) for r in closed]
    returns = [float(r.get("return_pct") or 0) for r in closed]
    return {
        "trades": len(closed),
        "win_rate": len(wins) / len(closed),
        "profit_factor": (gross_win / gross_loss) if gross_loss else None,
        "expectancy": sum(pnls) / len(closed),
        "avg_win": (sum(returns[: len(wins)]) / len(wins)) if wins else None,
        "avg_return": sum(returns) / len(closed),
        "avg_hold": sum(holds) / len(holds),
        "total_pnl": sum(pnls),
        "best": max(pnls), "worst": min(pnls),
        "gross_win": gross_win, "gross_loss": gross_loss,
    }


# ---------------------------------------------------------------- performance

def performance_view(settings: Settings, db: Database) -> dict:
    equity = [{"day": r["day"], "nav": r["nav"], "cash": r["cash"],
               "exposure": (r["positions_value"] / r["nav"]) if r["nav"] else 0.0}
              for r in db.equity_series()]
    closed = [dict(r) for r in db.closed_trades()]
    for row in closed:
        row["return_pct"] = (row["pnl"] or 0) / max(
            (row["entry_price"] or 0) * (row["shares"] or 0), 1e-9)
    stats = trade_stats(closed)

    yearly: dict[str, dict] = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "wins": 0})
    for row in closed:
        key = str(row["exit_date"])[:4] or "—"
        bucket = yearly[key]
        bucket["pnl"] += float(row["pnl"] or 0)
        bucket["trades"] += 1
        bucket["wins"] += 1 if (row["pnl"] or 0) > 0 else 0
    year_rows = [{"year": y, **v} for y, v in sorted(yearly.items())]

    by_reason: dict[str, dict] = defaultdict(lambda: {"count": 0, "pnl": 0.0})
    for row in closed:
        bucket = by_reason[str(row["exit_reason"] or "—")]
        bucket["count"] += 1
        bucket["pnl"] += float(row["pnl"] or 0)
    reason_rows = [
        {"reason": r, "label": reason_label(r),
         "count": v["count"], "pnl": v["pnl"],
         "share": v["count"] / len(closed) if closed else 0}
        for r, v in sorted(by_reason.items(), key=lambda kv: -kv[1]["count"])
    ]

    drawdown = None
    if equity:
        peak = equity[0]["nav"]
        worst = 0.0
        for point in equity:
            peak = max(peak, point["nav"])
            worst = min(worst, point["nav"] / peak - 1.0 if peak else 0.0)
        drawdown = worst

    return {
        "page": "performance",
        "freshness": freshness(settings, db),
        "equity": equity,
        "stats": stats,
        "yearly": year_rows,
        "reasons": reason_rows,
        "max_drawdown": drawdown,
        "start": equity[0]["day"] if equity else None,
        "end": equity[-1]["day"] if equity else None,
    }


# ----------------------------------------------------------------------- data

def data_view(settings: Settings, db: Database, job: dict | None = None) -> dict:
    states = db.sync_states()
    counts: dict[str, int] = defaultdict(int)
    for row in states:
        counts[row["status"]] += 1
    stale = [dict(r) for r in states if r["status"] == "stale"][:25]
    suspicious = [dict(r) for r in states if r["status"] == "suspicious"][:25]
    failed = [dict(r) for r in states if r["status"] == "failed"][:25]
    last_log = db.last_sync_log()
    log_row = dict(last_log) if last_log else None
    failures_path = settings.universe_dir / "sync_failures.csv"
    failures = []
    if failures_path.exists():
        failures = failures_path.read_text(encoding="utf-8").splitlines()[:12]
    from ..live.ops import sync_lock_info

    cursor = db.get_state("tracker.last_day")
    trusted = db.trusted_day()
    pending = (sum(1 for d in db.trading_days(start=cursor, end=trusted) if d > cursor)
               if cursor and trusted else 0)
    return {
        "page": "data",
        "freshness": freshness(settings, db),
        "coverage": db.get_json("sync.coverage", {}) or {},
        "universe_day": db.universe_day(),
        "counts": dict(counts),
        "synced_symbols": sum(counts.values()),
        "sync_state_coverage": (
            f"{sum(counts.values())} of "
            f"{len(list(settings.daily_dir.glob('*.parquet')))}"),
        "trading_days": len(db.trading_days()),
        "flagged_total": sum(1 for s in states if s["status"] != "ok"),
        "stale": stale, "suspicious": suspicious, "failed": failed,
        "last_log": log_row,
        "lock": sync_lock_info(settings),
        "failures": failures,
        "schedule": settings.schedule,
        "job": job or {},
        "tracker_cursor": cursor,
        "pending_days": pending,
    }


# ------------------------------------------------------------------- settings

def settings_view(settings: Settings, db: Database) -> dict:
    live = load_live_settings(settings, db)
    return {
        "page": "settings",
        "freshness": freshness(settings, db),
        "live": live,
        "schedule": settings.schedule,
        "auto_sync_disabled": bool(db.get_state("app.autosync_disabled")),
        "root": str(settings.root),
    }


def closed_csv(rows) -> str:
    """CSV export of closed trades (also used by the CLI exporter)."""
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["symbol", "signal_date", "entry_date", "entry_price",
                     "shares", "exit_date", "exit_price", "exit_reason",
                     "days_held", "pnl", "return_pct", "stop_at_exit"])
    for row in rows:
        shares = int(row["shares"] or 0)
        entry = float(row["entry_price"] or 0)
        pnl = float(row["pnl"] or 0)
        writer.writerow([
            row["symbol"], row["signal_date"], row["entry_date"],
            f"{entry:.2f}", shares, row["exit_date"],
            f"{float(row['exit_price'] or 0):.2f}", row["exit_reason"],
            row["days_held"], f"{pnl:.2f}",
            f"{(pnl / (entry * shares) if entry and shares else 0):.4f}",
            f"{float(row['stop_live'] or 0):.2f}",
        ])
    return buffer.getvalue()


def data_age_days(path: Path) -> int | None:
    if not path.exists():
        return None
    return (Date.today() - last_stored_date(path)).days if last_stored_date(path) else None
