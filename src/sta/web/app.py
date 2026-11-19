"""Local FastAPI app: today's signals, paper tracker, performance, data ops.

Server-rendered Jinja templates + HTMX for the small interactions. No build
step, no CDN, no auth: it binds to 127.0.0.1 and assumes one trusted user.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import date as Date
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..core.config import Settings, load_settings
from ..data.sync import DataSync
from ..live.db import Database
from ..live.settings import load_live_settings, save_live_settings
from ..live.signals import run_scan
from ..live.tracker import Tracker
from .jobs import JobManager
from .pages import (
    closed_csv,
    data_view,
    day_label,
    inr,
    pct,
    performance_view,
    reason_label,
    settings_view,
    short_day,
    signal_view,
    today_view,
    tone,
    tracker_view,
)

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
TEMPLATES = HERE / "templates"
STATIC = HERE / "static"


def create_app(settings: Settings | None = None,
               *, auto_sync: bool = True) -> FastAPI:
    settings = settings or load_settings()
    settings.ensure_dirs()
    db = Database(settings.db_path)
    jobs = JobManager()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        db.init()
        db.reap_sync_logs()
        _maybe_start_catch_up(settings, db, jobs, enabled=auto_sync)
        yield

    app = FastAPI(title="STA paper desk", lifespan=lifespan,
                  docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.db = db
    app.state.jobs = jobs

    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATES))
    templates.env.filters.update(
        money=inr, pct=pct, day=day_label, short_day=short_day, tone=tone,
        reason=reason_label)
    templates.env.globals["today"] = lambda: Date.today().isoformat()
    templates.env.globals["asset_version"] = lambda name: int(
        (STATIC / name).stat().st_mtime) if (STATIC / name).exists() else 0

    def render(request: Request, name: str, context: dict, status: int = 200):
        return templates.TemplateResponse(request, name, context,
                                          status_code=status)

    def job_banner(request: Request, started: bool):
        return render(request, "partials/job.html",
                      {"job": jobs.snapshot(), "rejected": not started,
                       "armed": True})

    # ------------------------------------------------------------------ pages

    @app.get("/", response_class=HTMLResponse)
    def today(request: Request):
        return render(request, "today.html",
                      today_view(settings, db, jobs.snapshot()))

    @app.get("/tracker", response_class=HTMLResponse)
    def tracker_page(request: Request, year: str | None = None,
                     reason: str | None = None, result: str | None = None):
        return render(request, "tracker.html",
                      tracker_view(settings, db, year=year, reason=reason,
                                   result=result, job=jobs.snapshot()))

    @app.get("/performance", response_class=HTMLResponse)
    def performance(request: Request):
        return render(request, "performance.html",
                      performance_view(settings, db))

    @app.get("/data", response_class=HTMLResponse)
    def data_page(request: Request):
        return render(request, "data.html",
                      data_view(settings, db, jobs.snapshot()))

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, saved: int = 0):
        context = settings_view(settings, db)
        context["saved"] = bool(saved)
        return render(request, "settings.html", context)

    @app.post("/settings")
    def save_settings(position_size: float = Form(...),
                      max_positions: int = Form(...),
                      auto_track: str | None = Form(None),
                      scan_top_n: int = Form(...)):
        if not 1_000 <= position_size <= 10_000_000:
            raise HTTPException(422, "position size must be ₹1,000–₹1,00,00,000")
        if not 1 <= max_positions <= 20:
            raise HTTPException(422, "slots must be 1–20")
        if not 1 <= scan_top_n <= 50:
            raise HTTPException(422, "suggestions per session must be 1–50")
        save_live_settings(db, position_size=position_size,
                           max_positions=max_positions,
                           auto_track=bool(auto_track),
                           scan_top_n=scan_top_n)
        return RedirectResponse("/settings?saved=1", status_code=303)

    # ------------------------------------------------------------ interactions

    @app.post("/signals/{signal_id}/track", response_class=HTMLResponse)
    def track_signal(request: Request, signal_id: int):
        row = db.signal(signal_id)
        if row is None:
            raise HTTPException(404, "signal not found")
        if db.trade_for_signal(signal_id) is None:
            db.set_signal_action(signal_id, "track")
            db.insert_trade({"signal_id": signal_id, "symbol": row["symbol"],
                             "exchange": row["exchange"], "status": "suggested",
                             "signal_date": row["signal_date"]})
        return render(request, "partials/signal_row.html",
                      {"row": signal_view(db, row)})

    @app.post("/signals/{signal_id}/skip", response_class=HTMLResponse)
    def skip_signal(request: Request, signal_id: int):
        row = db.signal(signal_id)
        if row is None:
            raise HTTPException(404, "signal not found")
        db.set_signal_action(signal_id, "skip")
        trade = db.trade_for_signal(signal_id)
        if trade and trade["status"] == "suggested":
            db.update_trade(trade["id"], status="cancelled")
        return render(request, "partials/signal_row.html",
                      {"row": signal_view(db, row)})

    # ------------------------------------------------------------------- jobs

    @app.post("/runs/scan", response_class=HTMLResponse)
    def start_scan(request: Request):
        live = load_live_settings(settings, db)
        started = jobs.start("scan", "Scanning the latest session",
                             lambda: run_scan(settings, db, live, commit=True).as_dict())
        return job_banner(request, started)

    @app.post("/runs/sync", response_class=HTMLResponse)
    def start_sync(request: Request):
        started = jobs.start("sync", "Refreshing price history",
                             lambda: _sync_then_track(settings, db, jobs.report))
        return job_banner(request, started)

    @app.post("/runs/track", response_class=HTMLResponse)
    def start_track(request: Request):
        started = jobs.start("track", "Replaying missed sessions",
                             lambda: _track(settings, db))
        return job_banner(request, started)

    @app.get("/partials/job", response_class=HTMLResponse)
    def job_partial(request: Request, poll: int = 0):
        snapshot = jobs.snapshot()
        response = render(request, "partials/job.html",
                          {"job": snapshot, "armed": True})
        if poll and snapshot["status"] in ("done", "error"):
            response.headers["HX-Refresh"] = "true"
        return response

    @app.get("/api/jobs")
    def api_jobs():
        return JSONResponse(jobs.snapshot())

    @app.get("/api/health")
    def api_health():
        return JSONResponse({"ok": True, "db": str(db.path),
                             "symbols": len(list(settings.daily_dir.glob("*.parquet")))})

    @app.post("/data/unlock")
    def unlock(request: Request):
        from ..live.ops import clear_sync_lock

        clear_sync_lock(settings)
        return RedirectResponse("/data", status_code=303)

    # ----------------------------------------------------------------- export

    @app.get("/export/closed.csv")
    def export_closed():
        content = closed_csv(db.closed_trades())
        return Response(content, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition":
                                 'attachment; filename="sta-closed-trades.csv"'})

    return app


# --------------------------------------------------------------------- jobs


def _sync_then_track(settings: Settings, db: Database,
                     progress=None) -> dict:
    from ..live.ops import check_sync_health

    result = DataSync(settings, db, progress=progress).run()
    check_sync_health(settings, result)
    tracked = _track(settings, db)
    return {"sync": result.as_dict(), "tracker": tracked}


def _catch_up_pipeline(settings: Settings, db: Database, progress=None) -> dict:
    """The daily sequence, used when the app starts on stale data."""
    from ..live.ops import check_sync_health

    result = DataSync(settings, db, progress=progress).run()
    check_sync_health(settings, result)
    scanned = None
    if result.updated or result.refreshed:
        scanned = run_scan(settings, db, load_live_settings(settings, db),
                           commit=True).as_dict()
    tracked = _track(settings, db)
    return {"sync": result.as_dict(), "scan": scanned, "tracker": tracked}


def _track(settings: Settings, db: Database) -> dict:
    tracker = Tracker(settings, db)
    days = tracker.catch_up()
    return {"days": len(days), "last": days[-1].as_dict() if days else None}


def _maybe_start_catch_up(settings: Settings, db: Database, jobs: JobManager,
                          enabled: bool) -> None:
    """Belt-and-braces: if the cache is behind, sync (and replay) on startup."""
    if not enabled or os.environ.get("STA_AUTOSYNC", "1") == "0":
        return
    if db.get_state("app.autosync_disabled") == "1":
        return
    if not any(settings.daily_dir.glob("*.parquet")):
        return
    from .pages import freshness

    state = freshness(settings, db)
    if state["status"] == "fresh":
        return
    log.info("startup: data is stale (%s), kicking a catch-up run", state["session"])
    jobs.start("sync", "Catching up after startup",
               lambda: _catch_up_pipeline(settings, db, jobs.report))


def serve(settings: Settings | None = None, host: str = "127.0.0.1",
          port: int = 8000) -> None:
    import uvicorn

    settings = settings or load_settings()
    from ..live.ops import setup_file_logging

    setup_file_logging(settings)
    logging.getLogger("sta").setLevel(logging.INFO)
    logging.getLogger("sta").info("serving on http://%s:%d", host, port)
    uvicorn.run(create_app(settings), host=host, port=port, log_level="info")


def app_factory() -> FastAPI:
    """``uvicorn sta.web.app:app_factory --factory`` entry point."""
    return create_app()
