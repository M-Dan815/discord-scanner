"""
parser.py
---------
Parses raw Discord message text into structured signal dictionaries.

Handles five distinct channel-type templates with flexible emoji/price extraction.
Timestamp parsing handles Discord's multi-line header format.
"""

import re
import logging
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Emoji signal mapping: Unicode circle → signal label
# ---------------------------------------------------------------------------
EMOJI_SIGNAL_MAP = {
    "🟢": "BIG_GREEN",
    "🔴": "BIG_RED",
    "🟡": "GOLDEN",
    "🟠": "BIG_ORANGE",
    "🔵": "BIG_BLUE",
    "⚪": "BIG_WHITE",
    "🟣": "BIG_PURPLE",
}

# Fallback: keyword → signal label (for channels where emoji may be absent)
KEYWORD_SIGNAL_MAP = {
    "GOLDEN": "GOLDEN",
    "BIG": "BIG",
}

# Timezone assumption for "today" / "yesterday" relative timestamps.
LOCAL_TZ = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Compiled patterns — one per channel template
# ---------------------------------------------------------------------------

# Shared sub-patterns
_TICKER = r"(?P<ticker>[A-Z0-9]+(?:\.[A-Z]+)?)"
_EMOJI = r"(?P<emoji>[🟢🔴🟡🟠🔵⚪🟣])?"
_PRICE = r"(?P<price>\d+(?:\.\d+)?)"

# Pattern 1: 4-hour
_P_4HOUR = re.compile(
    rf"{_TICKER}\s+is\s+showing\s+a\s+BIG\s+{_EMOJI}\s*on\s+the\s+4\s+hour\s+chart(?:\s+at\s+{_PRICE})?",
    re.IGNORECASE,
)

# Pattern 2: Daily gold-dot
_P_DAILY_GOLDEN = re.compile(
    rf"{_TICKER}\s+is\s+showing\s+a\s+GOLDEN\s+{_EMOJI}\s*on\s+the\s+DAILY\s+chart",
    re.IGNORECASE,
)

# Pattern 3: Daily EMA-ribbon
_P_DAILY_EMA = re.compile(
    rf"{_TICKER}\s+is\s+showing\s+a\s+{_EMOJI}\s*on\s+the\s+DAILY\s+CHART(?:\s+at\s+{_PRICE})?",
    re.IGNORECASE,
)

# Pattern 4: Monthly
_P_MONTHLY = re.compile(
    rf"{_TICKER}\s+is\s+showing\s+a\s+{_EMOJI}\s*on\s+the\s+MONTHLY\s+chart",
    re.IGNORECASE,
)

# Pattern 5: Weekly
_P_WEEKLY = re.compile(
    rf"{_TICKER}\s+is\s+showing\s+{_EMOJI}\s*on\s+the\s+WEEKLY\s+chart",
    re.IGNORECASE,
)

# Ordered list: most-specific first
PATTERNS = [
    ("4H",       "4-hour",       _P_4HOUR),
    ("1D",       "daily-golden", _P_DAILY_GOLDEN),
    ("1D",       "daily-ema",    _P_DAILY_EMA),
    ("1M",       "monthly",      _P_MONTHLY),
    ("1W",       "weekly",       _P_WEEKLY),
]

# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------

# "OverkillAPP — 1:00 AM"
_TS_TODAY = re.compile(
    r"overkill\w*\s*[—–-]\s*(?P<time>\d{1,2}:\d{2}\s*[AP]M)",
    re.IGNORECASE,
)

# "OVERKILLAPP — 7/2/26, 5:00 AM"
_TS_DATE = re.compile(
    r"overkill\w*\s*[—–-]\s*(?P<date>\d{1,2}/\d{1,2}/\d{2,4}),\s*(?P<time>\d{1,2}:\d{2}\s*[AP]M)",
    re.IGNORECASE,
)

# "OVERKILLAPP — Yesterday at 6:30 PM"
_TS_YESTERDAY = re.compile(
    r"overkill\w*\s*[—–-]\s*Yesterday\s+at\s+(?P<time>\d{1,2}:\d{2}\s*[AP]M)",
    re.IGNORECASE,
)


def parse_timestamp(header_text: str) -> Optional[datetime]:
    """
    Parse a Discord message header into a timezone-aware datetime.
    Handles newlines and various Discord timestamp formats.
    """
    if not header_text:
        return None
    
    # Strip newlines and normalize whitespace
    normalized = " ".join(header_text.split())
    
    now = datetime.now(tz=LOCAL_TZ)

    # Pattern 1: Full date with time "4/16/26, 6:30 PM" or "7/2/26, 5:00 AM"
    date_time = re.search(
        r'(\d{1,2}/\d{1,2}/\d{2,4}),?\s+(\d{1,2}:\d{2}\s*[AP]M)',
        normalized,
        re.IGNORECASE
    )
    if date_time:
        date_str = date_time.group(1)
        time_str = date_time.group(2).strip()
        try:
            parts = date_str.split("/")
            if len(parts) == 3 and len(parts[2]) == 2:
                parts[2] = "20" + parts[2]
            date_str = "/".join(parts)
            dt = datetime.strptime(f"{date_str} {time_str}", "%m/%d/%Y %I:%M %p")
            return dt.replace(tzinfo=LOCAL_TZ)
        except ValueError as e:
            logger.debug("Date parse failed for '%s': %s", normalized[:100], e)

    # Pattern 2: "Yesterday at 9:50 PM"
    yesterday_match = re.search(
        r'Yesterday\s+at\s+(\d{1,2}:\d{2}\s*[AP]M)',
        normalized,
        re.IGNORECASE
    )
    if yesterday_match:
        time_str = yesterday_match.group(1).strip()
        try:
            t = datetime.strptime(time_str, "%I:%M %p")
            yesterday = (now - timedelta(days=1)).date()
            return datetime(
                yesterday.year, yesterday.month, yesterday.day,
                t.hour, t.minute, tzinfo=LOCAL_TZ
            )
        except ValueError as e:
            logger.debug("Yesterday parse failed: %s", e)

    # Pattern 3: Full description "Friday, July 10, 2026 at 6:30 PM"
    full_description = re.search(
        r'([A-Z][a-z]+day),\s+([A-Z][a-z]+)\s+(\d{1,2}),\s+(\d{4})\s+at\s+(\d{1,2}:\d{2}\s*[AP]M)',
        normalized,
        re.IGNORECASE
    )
    if full_description:
        month_name = full_description.group(2)
        day = full_description.group(3)
        year = full_description.group(4)
        time_str = full_description.group(5).strip()
        try:
            date_str = f"{month_name} {day}, {year} {time_str}"
            dt = datetime.strptime(date_str, "%B %d, %Y %I:%M %p")
            return dt.replace(tzinfo=LOCAL_TZ)
        except ValueError as e:
            logger.debug("Full description parse failed: %s", e)

    # Pattern 4: Standalone time (assume today) "6:30 PM" or "[ 5:01 AM ]"
    time_only = re.search(
        r'(?:\[?\s*)?(\d{1,2}:\d{2}\s*[AP]M)(?:\s*\])?',
        normalized,
        re.IGNORECASE
    )
    if time_only:
        time_str = time_only.group(1).strip()
        try:
            t = datetime.strptime(time_str, "%I:%M %p")
            return datetime(
                now.year, now.month, now.day,
                t.hour, t.minute, tzinfo=LOCAL_TZ
            )
        except ValueError as e:
            logger.debug("Time-only parse failed: %s", e)
    
    logger.debug("Could not parse timestamp from: %r", normalized[:100])
    return None


def _resolve_signal(emoji_char: Optional[str], keyword: Optional[str], channel_signal_type: str, channel_name: str) -> str:
    """
    Convert an extracted emoji character (and optional keyword) to a
    human-readable signal label.
    
    Priority order:
    1. If channel config forces a specific signal_type (e.g. "GOLDEN"), use that
    2. If emoji is detected, use emoji mapping
    3. If channel name contains "red" → BIG_RED
    4. If channel name contains "green" → BIG_GREEN
    5. Fallback to UNKNOWN
    """
    # If channel config forces a specific signal type, use it
    if channel_signal_type and channel_signal_type != "AUTO":
        return channel_signal_type
    
    # Try emoji first
    if emoji_char and emoji_char in EMOJI_SIGNAL_MAP:
        return EMOJI_SIGNAL_MAP[emoji_char]
    
    # Try keyword
    if keyword:
        kw = keyword.upper()
        if kw in KEYWORD_SIGNAL_MAP:
            return KEYWORD_SIGNAL_MAP[kw]
    
    # Infer from channel name
    ch_lower = channel_name.lower()
    if "red" in ch_lower:
        return "BIG_RED"
    if "green" in ch_lower:
        return "BIG_GREEN"
    
    return "UNKNOWN"


def parse_message(
    raw_text: str,
    channel_config: dict,
    header_text: str = "",
) -> Optional[dict]:
    """
    Parse a single Discord message into a structured signal dict.

    Args:
        raw_text:       The message body text (may be multi-line).
        channel_config: The channel's config dict (name, timeframe, signal_type, url).
        header_text:    The timestamp header line.

    Returns a dict or None if no signal pattern matched:
        {
            "ticker":       "XLMUSD",
            "price":        0.180132,       # None if not in template
            "signal":       "BIG_GREEN",
            "timeframe":    "4H",
            "channel_name": "4-hour-alert",
            "template":     "4-hour",
            "timestamp":    datetime(...),  # None if unparseable
            "raw_text":     "...",
        }
    """
    # Normalise text: collapse whitespace runs
    text = " ".join(raw_text.split())

    matched_timeframe = None
    matched_template = None
    matched_ticker = None
    matched_price = None
    matched_emoji = None

    for timeframe, template_name, pattern in PATTERNS:
        m = pattern.search(text)
        if m:
            matched_timeframe = timeframe
            matched_template = template_name
            matched_ticker = m.group("ticker").upper()

            # Price group may not exist in all patterns
            try:
                price_str = m.group("price")
                matched_price = float(price_str) if price_str else None
            except (IndexError, AttributeError):
                matched_price = None

            # Emoji group is always optional
            try:
                matched_emoji = m.group("emoji")
            except (IndexError, AttributeError):
                matched_emoji = None

            break  # first match wins

    if matched_ticker is None:
        logger.debug("No signal pattern matched in: %.120s", text)
        return None

    # Override timeframe from channel config if available
    config_timeframe = channel_config.get("timeframe")
    if config_timeframe and config_timeframe != matched_timeframe:
        logger.debug(
            "Timeframe mismatch: parsed=%s, config=%s — using config value",
            matched_timeframe, config_timeframe,
        )
        matched_timeframe = config_timeframe

    # Resolve signal type (now with channel name fallback)
    config_signal = channel_config.get("signal_type", "AUTO")
    channel_name = channel_config.get("name", "")
    signal = _resolve_signal(matched_emoji, None, config_signal, channel_name)

    # Parse timestamp
    timestamp = parse_timestamp(header_text) if header_text else None

    return {
        "ticker": matched_ticker,
        "price": matched_price,
        "signal": signal,
        "timeframe": matched_timeframe,
        "channel_name": channel_name,
        "template": matched_template,
        "timestamp": timestamp,
        "raw_text": raw_text,
    }