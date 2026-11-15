"""Configuration loading. Single source of truth: ``config/settings.toml``."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

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


@dataclass(frozen=True)
class Settings:
    root: Path
    universe: UniverseConfig
    download: DownloadConfig

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def universe_dir(self) -> Path:
        return self.data_dir / "universe"

    def ensure_dirs(self) -> None:
        self.universe_dir.mkdir(parents=True, exist_ok=True)


def load_settings(path: Optional[Path] = None) -> Settings:
    cfg_path = path or DEFAULT_CONFIG
    raw: dict = {}
    if cfg_path.exists():
        with open(cfg_path, "rb") as fh:
            raw = tomllib.load(fh)

    uni_raw = raw.get("universe", {})
    dl_raw = raw.get("download", {})
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
        ),
    )
