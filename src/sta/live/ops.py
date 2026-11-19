"""Operational helpers: rotating logs, database backups, desktop alerts.

Everything here is best-effort: a failed toast or a locked database file must
never break a sync or a scan, so failures are logged and swallowed.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import sys
from datetime import date as Date
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from ..core.config import Settings
from .db import Database

log = logging.getLogger(__name__)

BACKUP_KEEP = 14
_LOG_HANDLER_FLAG = "_sta_file_handler"


# ---------------------------------------------------------------------- logs


def setup_file_logging(settings: Settings) -> Path:
    """Add one rotating file handler (``data/logs/sta.log``) to the app logger."""
    logs_dir = settings.logs_dir
    logs_dir.mkdir(parents=True, exist_ok=True)
    path = logs_dir / "sta.log"
    root = logging.getLogger("sta")
    if any(getattr(h, _LOG_HANDLER_FLAG, False) for h in root.handlers):
        return path
    handler = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024,
                                  backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.setLevel(logging.INFO)
    setattr(handler, _LOG_HANDLER_FLAG, True)
    root.addHandler(handler)
    return path


# ------------------------------------------------------------------- backups


def backup_db(settings: Settings, keep: int = BACKUP_KEEP) -> Path | None:
    """Consistent copy of ``data/app.db`` into ``data/backups/`` (keeps 14)."""
    source = settings.db_path
    if not source.exists():
        log.info("backup skipped: no database yet")
        return None
    settings.backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = settings.backups_dir / f"app_{stamp}.db"
    try:
        # sqlite's own backup API is safe while WAL readers are active
        with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
            src.backup(dst)
    except Exception:  # noqa: BLE001 — fall back to a file copy
        shutil.copy2(source, target)
    _prune(settings.backups_dir, keep)
    log.info("database backup written: %s", target.name)
    return target


def _prune(directory: Path, keep: int) -> None:
    backups = sorted(directory.glob("app_*.db"), key=lambda p: p.stat().st_mtime,
                     reverse=True)
    for stale in backups[keep:]:
        stale.unlink(missing_ok=True)


# -------------------------------------------------------------------- alerts


def alert(settings: Settings, title: str, message: str) -> None:
    """Log an alert and (on Windows) raise a desktop notification."""
    log.warning("ALERT %s: %s", title, message)
    alerts_log = settings.logs_dir / "alerts.log"
    try:
        settings.logs_dir.mkdir(parents=True, exist_ok=True)
        with open(alerts_log, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().isoformat(timespec='seconds')}\t{title}\t{message}\n")
    except OSError:
        pass
    if sys.platform != "win32" or os.environ.get("STA_NO_TOAST") == "1":
        return
    _toast(title, message)


def _toast(title: str, message: str) -> None:
    import subprocess

    safe_title = title.replace("'", "''")
    safe_message = message.replace("'", "''")
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "Add-Type -AssemblyName System.Drawing; "
        "$n = New-Object System.Windows.Forms.NotifyIcon; "
        "$n.Icon = [System.Drawing.SystemIcons]::Warning; "
        "$n.Visible = $true; "
        f"$n.ShowBalloonTip(8000, '{safe_title}', '{safe_message}', 'Warning'); "
        "Start-Sleep -Seconds 2; $n.Dispose()"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-WindowStyle", "Hidden",
                        "-Command", script],
                       timeout=20, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:  # noqa: BLE001 — a toast is never critical
        log.debug("desktop toast failed: %s", exc)


def check_sync_health(settings: Settings, result) -> None:
    """>10% of symbols failing is worth interrupting the user for."""
    if not result.symbols:
        return
    share = result.failed / result.symbols
    if share > 0.10:
        alert(settings, "Price sync degraded",
              f"{result.failed} of {result.symbols} symbols failed "
              f"({share:.0%}). Signals may be incomplete.")


def check_mark_health(settings: Settings, db: Database, as_of,
                      stale_sessions: int = 5) -> None:
    """Alert when an open position has not been marked for 5+ sessions."""
    days = db.trading_days(end=as_of)
    if len(days) <= stale_sessions:
        return
    limit = days[-(stale_sessions + 1)]
    marks = db.latest_marks()
    stuck = []
    for row in db.open_trades():
        mark = marks.get(row["id"])
        if mark is None or str(mark["date"]) < limit:
            stuck.append(row["symbol"])
    if stuck:
        alert(settings, "Positions cannot be marked",
              f"{len(stuck)} open position(s) have no mark for "
              f"{stale_sessions}+ sessions: {', '.join(stuck[:5])}")


def today_stamp() -> str:
    return Date.today().strftime("%Y%m%d")


# ----------------------------------------------------------------- book size


def rebase_book(settings: Settings, db: Database, *, position_size: float,
                max_positions: int) -> dict:
    """Change the paper book size and restart the equity baseline.

    Only safe while nothing is open: the cash ledger and the equity curve are
    reset to ``position_size * max_positions`` and the last session is replayed
    so the curve starts from the new capital. Queued suggestions are untouched.
    """
    from .settings import save_live_settings
    from .tracker import CASH_KEY, CURSOR_KEY, Tracker

    if db.open_trades():
        raise ValueError("close or cancel open positions before resizing the book")

    book = position_size * max_positions
    save_live_settings(db, position_size=position_size,
                       max_positions=max_positions)
    db.set_state(CASH_KEY, book)
    with db.connect() as conn:
        conn.execute("DELETE FROM equity")
    db.set_state(CURSOR_KEY, "")
    last = db.trusted_day() or db.last_day()
    replayed = None
    if last:
        replayed = Tracker(settings, db).update_day(last).as_dict()
    log.info("book rebased to %s x %s = %s", position_size, max_positions, book)
    return {"position_size": position_size, "max_positions": max_positions,
            "book": book, "replayed": replayed}


# --------------------------------------------------------------- sync lock


def sync_lock_info(settings: Settings) -> dict | None:
    """Describe the lock file when one is held (freshness of a crashed run)."""
    path = settings.data_dir / ".sync.lock"
    if not path.exists():
        return None
    import time

    from ..data.sync import lock_owner_pid, pid_alive

    try:
        age = max(0.0, time.time() - path.stat().st_mtime)
        detail = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    owner = lock_owner_pid(path)
    orphaned = owner is not None and not pid_alive(owner)
    return {"age_minutes": round(age / 60, 1),
            "stale": age > 15 * 60 or orphaned,
            "orphaned": orphaned,
            "detail": detail}


def clear_sync_lock(settings: Settings, force: bool = False) -> bool:
    """Remove the lock file; without ``force`` only when it is clearly stale."""
    info = sync_lock_info(settings)
    if info is None:
        return False
    if not info["stale"] and not force:
        return False
    (settings.data_dir / ".sync.lock").unlink(missing_ok=True)
    log.warning("sync lock cleared (%s min old)", info["age_minutes"])
    return True
