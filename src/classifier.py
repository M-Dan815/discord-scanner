"""
classifier.py
-------------
Determines whether a ticker belongs to crypto or stock universe,
then resolves which section (holdings / watchlist / all) it sits in.
Handles auto-population of the 'all' section and persists changes.
"""

import json
import logging
import re
from pathlib import Path
from typing import Literal, Tuple
from threading import Lock

logger = logging.getLogger(__name__)

AssetType = Literal["crypto", "stock"]
Section = Literal["holdings", "watchlist", "all"]

# ---------------------------------------------------------------------------
# Suffix heuristic for first-seen classification
# ---------------------------------------------------------------------------
_CRYPTO_SUFFIX_RE = re.compile(
    r"(USDT|USD|\.D)$",
    re.IGNORECASE,
)

# Canonical base-ticker extraction: strip known crypto suffixes
def _strip_crypto_suffix(ticker: str) -> str:
    """Return the base asset symbol without the exchange-pair suffix."""
    cleaned = _CRYPTO_SUFFIX_RE.sub("", ticker.upper()).rstrip(".")
    return cleaned if cleaned else ticker.upper()


def classify_asset_type(ticker: str) -> Tuple[AssetType, str]:
    """
    Heuristic-first classification of a ticker.

    Returns:
        (asset_type, base_ticker)
        asset_type: "crypto" or "stock"
        base_ticker: the ticker with crypto suffix stripped (if applicable)

    Examples:
        "XLMUSD"  → ("crypto", "XLM")
        "APTUSDT" → ("crypto", "APT")
        "USDC.D"  → ("crypto", "USDC")
        "BA"      → ("stock",  "BA")
        "VIX"     → ("stock",  "VIX")
    """
    upper = ticker.upper()
    if _CRYPTO_SUFFIX_RE.search(upper):
        base = _strip_crypto_suffix(upper)
        return ("crypto", base)
    return ("stock", upper)


# ---------------------------------------------------------------------------
# TickerStore: loads, queries, and persists both ticker JSON files
# ---------------------------------------------------------------------------

class TickerStore:
    """
    Thread-safe in-memory store for both ticker files.
    Exposes lookup and auto-add operations.
    """

    def __init__(self, crypto_path: Path, stock_path: Path):
        self._crypto_path = crypto_path
        self._stock_path = stock_path
        self._lock = Lock()
        self._data: dict[AssetType, dict] = {
            "crypto": self._load(crypto_path),
            "stock": self._load(stock_path),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _load(path: Path) -> dict:
        """Load a ticker JSON file, creating it with defaults if absent."""
        if not path.exists():
            logger.warning("Ticker file not found, creating: %s", path)
            path.parent.mkdir(parents=True, exist_ok=True)
            default = {"holdings": [], "watchlist": [], "all": []}
            path.write_text(json.dumps(default, indent=2))
            return default

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Ensure all three sections exist
        for section in ("holdings", "watchlist", "all"):
            data.setdefault(section, [])
        return data

    def _save(self, asset_type: AssetType) -> None:
        """Persist the in-memory data for one asset type back to disk."""
        path = self._crypto_path if asset_type == "crypto" else self._stock_path
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data[asset_type], f, indent=2)
        tmp.replace(path)  # atomic rename on POSIX
        logger.debug("Persisted %s ticker file.", asset_type)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve_section(
        self,
        ticker: str,
        asset_type: AssetType,
    ) -> Section:
        """
        Look up a base ticker (suffix already stripped) within the
        appropriate file and return its section.

        If the ticker is not found in holdings or watchlist, it is
        auto-appended to 'all' (if not already there) and persisted.

        Returns: "holdings" | "watchlist" | "all"
        """
        upper = ticker.upper()
        with self._lock:
            store = self._data[asset_type]

            # Normalise stored tickers to uppercase for comparison
            if upper in [t.upper() for t in store["holdings"]]:
                return "holdings"
            if upper in [t.upper() for t in store["watchlist"]]:
                return "watchlist"

            # Not found — ensure it lands in 'all'
            if upper not in [t.upper() for t in store["all"]]:
                store["all"].append(upper)
                logger.info(
                    "Auto-added '%s' to %s/all section.", upper, asset_type
                )
                self._save(asset_type)

            return "all"

    def is_known(self, ticker: str, asset_type: AssetType) -> bool:
        """Return True if ticker appears in ANY section of the given file."""
        upper = ticker.upper()
        with self._lock:
            store = self._data[asset_type]
            all_tickers = (
                [t.upper() for t in store["holdings"]]
                + [t.upper() for t in store["watchlist"]]
                + [t.upper() for t in store["all"]]
            )
            return upper in all_tickers

    def find_in_either_file(self, ticker: str) -> Tuple[AssetType, Section] | None:
        """
        Search both files for a ticker (base form, suffix already stripped).
        Returns (asset_type, section) if found, else None.
        Used to override heuristic classification when a ticker already
        exists in a file (manual override support).
        """
        upper = ticker.upper()
        for asset_type in ("crypto", "stock"):
            with self._lock:
                store = self._data[asset_type]
                for section in ("holdings", "watchlist", "all"):
                    if upper in [t.upper() for t in store[section]]:
                        return (asset_type, section)
        return None

    def reload(self) -> None:
        """Re-read both files from disk (useful if user edits them live)."""
        with self._lock:
            self._data["crypto"] = self._load(self._crypto_path)
            self._data["stock"] = self._load(self._stock_path)
        logger.info("Ticker files reloaded from disk.")


# ---------------------------------------------------------------------------
# High-level classify() function used by main orchestration
# ---------------------------------------------------------------------------

def classify(
    raw_ticker: str,
    store: TickerStore,
) -> Tuple[AssetType, str, Section]:
    """
    Full classification pipeline for a raw ticker string as extracted
    from a Discord message.

    Steps:
        1. Heuristic suffix detection → asset_type + base_ticker
        2. Override: check if base_ticker already exists in either file
           (respects manual overrides over heuristic)
        3. Resolve section within the correct file (auto-adds to 'all')

    Returns:
        (asset_type, base_ticker, section)

    Example:
        classify("XLMUSD", store) → ("crypto", "XLM", "watchlist")
        classify("BA",     store) → ("stock",  "BA",  "holdings")
        classify("NEWTKN", store) → ("crypto", "NEWTKN", "all")  ← auto-added
    """
    heuristic_type, base_ticker = classify_asset_type(raw_ticker)

    # Check if it's already explicitly placed in either file
    existing = store.find_in_either_file(base_ticker)
    if existing:
        found_type, found_section = existing
        if found_type != heuristic_type:
            logger.info(
                "Ticker '%s' found in %s file (overrides heuristic '%s').",
                base_ticker, found_type, heuristic_type,
            )
        return (found_type, base_ticker, found_section)

    # Not found anywhere — use heuristic type, resolve section (will auto-add to all)
    section = store.resolve_section(base_ticker, heuristic_type)
    return (heuristic_type, base_ticker, section)