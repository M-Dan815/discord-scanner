"""
main.py
-------
Orchestration entrypoint.

Supports two run modes:
  - "once": Run a single polling cycle and exit (for cron jobs)
  - "continuous": Poll continuously at configured interval (default)

Set RUN_MODE=once in .env for cron usage.
"""

import logging
import os
import signal
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

# Add src/ to path for sibling imports
sys.path.insert(0, str(Path(__file__).parent))

from classifier import TickerStore, classify
from notifier import Notifier
from parser import parse_message
from scanner import DiscordScanner
from scanner_config import load_channels, load_notification_prefs, should_notify

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

load_dotenv(Path(__file__).parent.parent / ".env")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "scanner.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent.parent
CONFIG_DIR = BASE_DIR / "config"
STATE_DIR = BASE_DIR / "state"
STATE_DIR.mkdir(exist_ok=True)

CRYPTO_TICKERS_PATH = CONFIG_DIR / "crypto_tickers.json"
STOCK_TICKERS_PATH  = CONFIG_DIR / "stock_tickers.json"
STATE_PATH          = STATE_DIR  / "last_seen_ids.json"

# ---------------------------------------------------------------------------
# Graceful shutdown flag
# ---------------------------------------------------------------------------

_shutdown_requested = False


def _handle_shutdown(signum, frame):
    global _shutdown_requested
    logger.info("Shutdown signal received (%s). Finishing current cycle...", signum)
    _shutdown_requested = True


signal.signal(signal.SIGINT,  _handle_shutdown)
signal.signal(signal.SIGTERM, _handle_shutdown)


# ---------------------------------------------------------------------------
# Per-message pipeline
# ---------------------------------------------------------------------------

def process_message(
    raw_msg: dict,
    channel_config: dict,
    ticker_store: TickerStore,
    prefs: dict,
    notifier: Notifier,
) -> None:
    """
    Full pipeline for a single raw Discord message.

    raw_msg keys: msg_id, channel_name, header_text, body_text
    """
    body         = raw_msg["body_text"]
    header       = raw_msg["header_text"]
    channel_name = raw_msg["channel_name"]

    # Step 1: Parse
    parsed = parse_message(body, channel_config, header_text=header)
    if parsed is None:
        logger.debug(
            "[%s] No signal pattern matched. Raw: %.120s", channel_name, body
        )
        return

    logger.info(
        "[%s] Parsed signal: ticker=%s, signal=%s, timeframe=%s, price=%s",
        channel_name, parsed["ticker"], parsed["signal"],
        parsed["timeframe"], parsed.get("price"),
    )

    # Step 2: Classify (asset type + section)
    asset_type, base_ticker, section = classify(parsed["ticker"], ticker_store)

    # Merge classification into parsed dict for downstream use
    signal_record = {
        **parsed,
        "base_ticker": base_ticker,
        "asset_type":  asset_type,
        "section":     section,
    }

    logger.info(
        "[%s] Classified: %s → %s/%s (section=%s)",
        channel_name, parsed["ticker"], asset_type, base_ticker, section,
    )

    # Step 3: Notification filter
    if not should_notify(signal_record, prefs):
        logger.info(
            "[%s] SUPPRESSED — section=%s, timeframe=%s (not in notify rules)",
            channel_name, section, signal_record["timeframe"],
        )
        return

    # Step 4: Queue / send alert
    logger.info(
        "[%s] MATCH — %s (%s/%s) → queuing alert.",
        channel_name, base_ticker, asset_type, section,
    )
    notifier.add_signal(signal_record)


# ---------------------------------------------------------------------------
# Single polling cycle
# ---------------------------------------------------------------------------

def run_once(
    scanner: DiscordScanner,
    channels: list[dict],
    ticker_store: TickerStore,
    prefs: dict,
    notifier: Notifier,
) -> int:
    """
    Execute one full polling cycle across all enabled channels.
    Returns total number of new messages processed.
    """
    channel_map = {ch["name"]: ch for ch in channels}

    total = 0
    for raw_msg in scanner.scrape_all_channels(channels):
        ch_config = channel_map.get(raw_msg["channel_name"], {})
        process_message(raw_msg, ch_config, ticker_store, prefs, notifier)
        total += 1

    # Flush batch at end of cycle (no-op in immediate mode)
    flushed = notifier.flush()
    if flushed:
        logger.info("Batch email sent: %d signal(s).", flushed)

    return total


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    logger.info("=" * 60)
    logger.info("Discord Signal Scanner starting up.")
    logger.info("=" * 60)

    # --- Load environment ---
    gmail_address    = os.getenv("GMAIL_ADDRESS", "")
    gmail_password   = os.getenv("GMAIL_APP_PASSWORD", "")
    discord_email    = os.getenv("DISCORD_EMAIL", "")
    discord_password = os.getenv("DISCORD_PASSWORD", "")
    user_data_dir    = os.getenv("PLAYWRIGHT_USER_DATA_DIR", "./browser_profile")
    poll_interval    = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
    run_mode         = os.getenv("RUN_MODE", "continuous")

    if not gmail_address or not gmail_password:
        logger.error(
            "GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set in .env"
        )
        sys.exit(1)

    # --- Load configs ---
    channels = load_channels()
    prefs    = load_notification_prefs()

    enabled_channels = [c for c in channels if c.get("enabled")]
    logger.info(
        "Channels: %d total, %d enabled.",
        len(channels), len(enabled_channels),
    )
    logger.info("Notify sections:   %s", prefs["notify_sections"])
    logger.info("Notify timeframes: %s", prefs["notify_timeframes"])
    logger.info("Email mode:        %s", prefs["email_mode"])

    # --- Resolve sender / recipient addresses ---
    sender_addr    = prefs.get("email_from", gmail_address)
    recipient_addr = prefs.get("email_to")

    if not recipient_addr:
        logger.error(
            "email_to is not set in notification_prefs.json. "
            "Please add a recipient address and restart."
        )
        sys.exit(1)

    if sender_addr != gmail_address:
        logger.warning(
            "email_from in notification_prefs.json ('%s') does not match "
            "GMAIL_ADDRESS in .env ('%s'). SMTP login always uses the .env "
            "value — update one of them to keep them consistent.",
            sender_addr, gmail_address,
        )

    logger.info(
        "Email routing: %s (sender/SMTP auth) → %s (recipient)",
        gmail_address, recipient_addr,
    )

    # --- Setup ticker store ---
    ticker_store = TickerStore(CRYPTO_TICKERS_PATH, STOCK_TICKERS_PATH)

    # --- Setup notifier ---
    notifier = Notifier(
        gmail_address=gmail_address,
        gmail_app_password=gmail_password,
        to_addr=recipient_addr,
        subject_prefix=prefs.get("email_subject_prefix", "[SignalScanner]"),
        mode=prefs.get("email_mode", "batch"),
    )

    # --- Scanner ---
    with DiscordScanner(
        user_data_dir=user_data_dir,
        state_path=STATE_PATH,
        headless=True,
    ) as scanner:

        # First-time login check
        if not scanner.ensure_logged_in():
            if not discord_email or not discord_password:
                logger.error(
                    "Not logged into Discord and no credentials in .env. "
                    "Set DISCORD_EMAIL and DISCORD_PASSWORD for first-time login."
                )
                sys.exit(1)
            logger.info("Performing first-time Discord login...")
            if not scanner.login(discord_email, discord_password):
                logger.error("Discord login failed. Exiting.")
                sys.exit(1)

        # --- Main loop (mode-dependent) ---
        if run_mode == "once":
            # Single-cycle mode (for cron jobs)
            logger.info("=== Single-cycle mode ===")
            try:
                processed = run_once(scanner, channels, ticker_store, prefs, notifier)
                logger.info("Cycle complete. Messages processed: %d.", processed)
            except Exception as e:
                logger.error("Error in polling cycle: %s", e, exc_info=True)
            
            # Final flush
            remaining = notifier.pending_count()
            if remaining:
                logger.info("Flushing %d pending signal(s)...", remaining)
                notifier.flush()
            
            logger.info("Single cycle complete. Exiting.")
        
        else:
            # Continuous mode (original behavior)
            logger.info("=== Continuous mode (polling every %ds) ===", poll_interval)
            cycle = 0
            ticker_reload_every = 10

            while not _shutdown_requested:
                cycle += 1
                logger.info("--- Polling cycle #%d ---", cycle)

                # Periodic ticker file reload
                if cycle % ticker_reload_every == 0:
                    logger.info("Reloading ticker files from disk...")
                    ticker_store.reload()

                # Reload channels + prefs each cycle
                try:
                    channels = load_channels()
                    prefs    = load_notification_prefs()
                except Exception as e:
                    logger.warning(
                        "Config reload error: %s — using cached config.", e
                    )

                try:
                    processed = run_once(
                        scanner, channels, ticker_store, prefs, notifier
                    )
                    logger.info(
                        "Cycle #%d complete. Messages processed: %d.", cycle, processed
                    )
                except Exception as e:
                    logger.error(
                        "Unhandled error in polling cycle: %s", e, exc_info=True
                    )

                if _shutdown_requested:
                    break

                logger.info("Sleeping %ds until next cycle...", poll_interval)
                for _ in range(poll_interval):
                    if _shutdown_requested:
                        break
                    time.sleep(1)

            # Final flush on shutdown
            remaining = notifier.pending_count()
            if remaining:
                logger.info(
                    "Flushing %d pending signal(s) before shutdown...", remaining
                )
                notifier.flush()

            logger.info("Scanner shut down cleanly.")


if __name__ == "__main__":
    main()