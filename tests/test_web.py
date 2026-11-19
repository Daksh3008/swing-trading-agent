"""D8: web smoke tests via Starlette's TestClient (no network, no browser)."""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from sta.core.config import DownloadConfig, Settings, UniverseConfig
from sta.live.db import Database
from sta.web.app import create_app
from sta.web.pages import _latest_business_day, inr, pct


@pytest.fixture()
def client(tmp_path):
    settings = Settings(root=tmp_path, universe=UniverseConfig(),
                        download=DownloadConfig())
    settings.ensure_dirs()

    # one cached symbol so /data and freshness have something to report
    dates = pd.bdate_range("2024-01-01", periods=30)
    frame = pd.DataFrame({
        "Date": dates, "Open": 100.0, "High": 101.0, "Low": 99.0,
        "Close": 100.0, "Volume": 1000, "Adj_Close": 100.0})
    frame.to_parquet(settings.daily_dir / "AAA.BO.parquet", index=False)
    pd.DataFrame([{"symbol": "AAA.BO", "exchange": "BSE", "mcap_cr": 900.0,
                   "cap_class": "micro"}]).to_parquet(
        settings.universe_dir / "membership.parquet", index=False)

    db = Database(settings.db_path).init()
    signal_id = db.insert_signal({
        "signal_date": "2024-02-09", "symbol": "AAA.BO", "exchange": "BSE",
        "name": "AAA", "score": 0.91, "close": 100.0, "cap_class": "micro",
        "entry_plan": 100.0, "stop_initial": 90.0, "qty": 150, "risk": 1500.0,
        "dist_high": -0.01, "action": "new"})
    db.upsert_days(["2024-02-08", "2024-02-09"])
    fresh_id = db.insert_signal({
        "signal_date": "2024-02-09", "symbol": "BBB.BO", "exchange": "BSE",
        "name": "BBB", "score": 0.88, "close": 50.0, "cap_class": "small",
        "entry_plan": 50.0, "stop_initial": 45.0, "qty": 300, "risk": 1500.0,
        "dist_high": -0.03, "action": "new"})
    trade_id = db.insert_trade({"signal_id": signal_id, "symbol": "AAA.BO",
                                "status": "open", "signal_date": "2024-02-08"})
    db.update_trade(trade_id, entry_date="2024-02-09", entry_price=100.0,
                    shares=150, stop_initial=90.0, stop_live=95.0,
                    target=130.0, high_water=112.0, entry_cost=20.0)
    db.upsert_mark(trade_id, "2024-02-09", 108.0, 95.0, 112.0, 1180.0)
    db.upsert_equity("2024-02-09", 45000.0, 16200.0, 61200.0, 1)
    db.set_state("cash", 45000.0)

    app = create_app(settings, auto_sync=False)
    with TestClient(app) as test_client:
        test_client.signal_id = fresh_id
        yield test_client


def test_pages_render(client):
    for path in ("/", "/tracker", "/performance", "/data", "/settings"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "<html" in response.text
    today = client.get("/").text
    assert "AAA" in today
    assert "Run scan" in today
    assert "Paper NAV" in today


def test_track_button_creates_suggested_trade(client):
    temp = client.post("/signals/99/track")
    assert temp.status_code == 404           # no such signal
    response = client.post(f"/signals/{client.signal_id}/track")
    assert response.status_code == 200
    assert "Queued for next open" in response.text


def test_settings_round_trip(client):
    response = client.post("/settings", data={
        "position_size": "20000", "max_positions": "3",
        "auto_track": "on", "scan_top_n": "6"}, follow_redirects=False)
    assert response.status_code == 303
    page = client.get("/settings").text
    assert "20000" in page
    assert "checked" in page


def test_settings_validation(client):
    response = client.post("/settings", data={
        "position_size": "10", "max_positions": "3",
        "scan_top_n": "6"})
    assert response.status_code == 422


def test_api_and_csv(client):
    jobs = client.get("/api/jobs").json()
    assert jobs["status"] in ("idle", "done", "error")
    health = client.get("/api/health").json()
    assert health["ok"] is True
    csv = client.get("/export/closed.csv")
    assert csv.status_code == 200
    assert "symbol,signal_date" in csv.text


def test_tracker_filters(client):
    assert client.get("/tracker?year=2024&result=win").status_code == 200
    assert client.get("/tracker?reason=stop_trailing").status_code == 200


def test_expected_session_respects_the_close():
    # before 16:00 IST the current session's close is not published yet
    assert _latest_business_day(date(2026, 11, 16),
                                datetime(2026, 11, 16, 10, 0)) == date(2026, 11, 13)
    assert _latest_business_day(date(2026, 11, 16),
                                datetime(2026, 11, 16, 18, 0)) == date(2026, 11, 16)
    # a Sunday always resolves to Friday
    assert _latest_business_day(date(2026, 11, 15),
                                datetime(2026, 11, 15, 18, 0)) == date(2026, 11, 13)


def test_indian_number_and_percent_formatting():
    assert inr(1234567) == "₹12,34,567"
    assert inr(999) == "₹999"
    assert inr(-50000) == "-₹50,000"
    assert inr(None) == "—"
    assert pct(0.1234) == "+12.3%"
    assert pct(-0.05) == "-5.0%"
    assert pct(0.62, 1, False) == "62.0%"
