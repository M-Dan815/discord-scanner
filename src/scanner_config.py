"""
scanner_config.py
-----------------
Loads and validates all configuration files.
Single source of truth for config access throughout the app.
"""

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).parent.parent / "config"


def _load_json(path: Path, label: str) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"{label} config file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_channels() -> list[dict]:
    """Return list of channel dicts from channels_config.json."""
    data = _load_json(CONFIG_DIR / "channels_config.json", "channels")
    channels = data.get("channels", [])
    for ch in channels:
        ch.setdefault("enabled", False)
        ch.setdefault("timeframe", "UNKNOWN")
        ch.setdefault("signal_type", "AUTO")
    return channels


def load_notification_prefs() -> dict:
    """Return notification preferences dict."""
    prefs = _load_json(CONFIG_DIR / "notification_prefs.json", "notification_prefs")
    # Validate and apply defaults
    prefs.setdefault("notify_sections", ["holdings", "watchlist"])
    prefs.setdefault("notify_timeframes", ["ALL"])
    prefs.setdefault("email_mode", "batch")
    prefs.setdefault("batch_interval_minutes", 15)
    prefs.setdefault("email_subject_prefix", "[SignalScanner]")

    # Normalise notify_timeframes to uppercase
    prefs["notify_timeframes"] = [t.upper() for t in prefs["notify_timeframes"]]
    return prefs


def should_notify(signal: dict, prefs: dict) -> bool:
    """
    Apply notification filter rules to a classified signal.

    Args:
        signal: dict with keys: section, timeframe, (others ignored here)
        prefs:  loaded notification_prefs dict

    Returns True if this signal should trigger an alert.
    """
    section_ok = signal.get("section") in prefs["notify_sections"]

    timeframes = prefs["notify_timeframes"]
    timeframe_ok = (
        "ALL" in timeframes
        or signal.get("timeframe", "").upper() in timeframes
    )

    return section_ok and timeframe_ok