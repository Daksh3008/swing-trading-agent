"""Configuration loading. Single source of truth: ``config/settings.toml``.

Runtime-editable knobs (position size, slots, auto-track) live in
``app_state`` and are merged over these defaults by ``sta.live.settings``.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = ROOT / "config" / "settings.toml"


@dataclass(frozen=True)
class UniverseConfig:
    tickers_file: str = "config/Equity.csv"  # relative to project root
    max_mcap_cr: float = 50_000.0            # ₹ crore cap on universe
    small_upper_cr: float = 25_000.0         # small-cap band upper bound
    micro_upper_cr: float = 5_000.0          # micro-cap band upper bound


@dataclass(frozen=True)
class DownloadConfig:
    period: str = "max"          # max historical data
    interval: str = "1d"
    workers: int = 8
    sleep: float = 0.25          # politeness delay per fetch (reference default)
    force: bool = False
    gap_fill: bool = True        # repair a missing BSE session from the NSE listing


@dataclass(frozen=True)
class LiveConfig:
    """Paper-trading defaults for the daily app."""

    position_size: float = 15_000.0
    max_positions: int = 4
    auto_track: bool = False
    scan_top_n: int = 10
    scan_tail_bars: int = 400    # bars loaded per symbol for a one-day scan
    stop_pct: float = 0.10
    target_pct: float = 0.30
    trailing_atr_mult: float = 3.0
    atr_window: int = 22


@dataclass(frozen=True)
class ScheduleConfig:
    time: str = "18:30"
    timezone: str = "Asia/Kolkata"


@dataclass(frozen=True)
class Settings:
    root: Path
    universe: UniverseConfig
    download: DownloadConfig
    live: LiveConfig = LiveConfig()
    schedule: ScheduleConfig = ScheduleConfig()

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def universe_dir(self) -> Path:
        return self.data_dir / "universe"

    @property
    def daily_dir(self) -> Path:
        return self.data_dir / "daily"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    def ensure_dirs(self) -> None:
        for path in (self.universe_dir, self.daily_dir, self.logs_dir,
                     self.backups_dir):
            path.mkdir(parents=True, exist_ok=True)


def load_settings(path: Path | None = None) -> Settings:
    cfg_path = path or DEFAULT_CONFIG
    raw: dict = {}
    if cfg_path.exists():
        with open(cfg_path, "rb") as fh:
            raw = tomllib.load(fh)

    uni_raw = raw.get("universe", {})
    dl_raw = raw.get("download", {})
    live_raw = raw.get("live", {})
    sched_raw = raw.get("schedule", {})
    return Settings(
        root=ROOT,
        universe=UniverseConfig(
            tickers_file=str(uni_raw.get("tickers_file", "config/Equity.csv")),
            max_mcap_cr=float(uni_raw.get("max_mcap_cr", 50_000.0)),
            small_upper_cr=float(uni_raw.get("small_upper_cr", 25_000.0)),
            micro_upper_cr=float(uni_raw.get("micro_upper_cr", 5_000.0)),
        ),
        download=DownloadConfig(
            period=str(dl_raw.get("period", "max")),
            interval=str(dl_raw.get("interval", "1d")),
            workers=int(dl_raw.get("workers", 8)),
            sleep=float(dl_raw.get("sleep", 0.25)),
            force=bool(dl_raw.get("force", False)),
            gap_fill=bool(dl_raw.get("gap_fill", True)),
        ),
        live=LiveConfig(
            position_size=float(live_raw.get("position_size", 15_000.0)),
            max_positions=int(live_raw.get("max_positions", 4)),
            auto_track=bool(live_raw.get("auto_track", False)),
            scan_top_n=int(live_raw.get("scan_top_n", 10)),
            scan_tail_bars=int(live_raw.get("scan_tail_bars", 400)),
            stop_pct=float(live_raw.get("stop_pct", 0.10)),
            target_pct=float(live_raw.get("target_pct", 0.30)),
            trailing_atr_mult=float(live_raw.get("trailing_atr_mult", 3.0)),
            atr_window=int(live_raw.get("atr_window", 22)),
        ),
        schedule=ScheduleConfig(
            time=str(sched_raw.get("time", "18:30")).replace('"', ""),
            timezone=str(sched_raw.get("timezone", "Asia/Kolkata")),
        ),
    )
