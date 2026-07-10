"""
scanner.py
----------
Playwright-based Discord channel reader.

Responsibilities:
  - Maintain a persistent browser profile (stays logged in).
  - For each enabled channel, navigate to its URL and scrape new messages.
  - Return raw (header, body) text pairs for each unseen message.
  - Track last-seen message IDs per channel to prevent duplicate processing.

Architecture notes:
  - Uses a SINGLE Playwright browser instance shared across all channels
    in a polling cycle to reduce memory overhead.
  - Navigates sequentially (not concurrently) to avoid rate-limiting.
  - Messages are identified by Discord's internal data-list-item-id attribute,
    which is stable across page loads.
  - Discord snowflake IDs are compared numerically (not lexicographically).
"""

import json
import logging
from pathlib import Path
from typing import Generator, Optional

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    sync_playwright,
    TimeoutError as PlaywrightTimeout,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CSS selectors — update if Discord changes its DOM structure
# ---------------------------------------------------------------------------

# Each message group container (header + body bundled together)
_SEL_MSG_GROUP = "li[id^='chat-messages-']"

# Within a group: the timestamp/author header
_SEL_HEADER = "h3[class*='header']"

# Within a group: the content spans that make up the message body
_SEL_CONTENT = "div[id^='message-content-']"

# Attribute holding the stable message ID
_ATTR_MSG_ID = "id"  # value like "chat-messages-CHANNEL_ID-MSG_ID"


def _extract_msg_id(element_id: str) -> str:
    """
    Extract the Discord snowflake message ID from the DOM element ID.
    e.g. "chat-messages-1234567890-9876543210" → "9876543210"
    """
    parts = element_id.rsplit("-", 1)
    return parts[-1] if len(parts) > 1 else element_id


# ---------------------------------------------------------------------------
# State persistence (last-seen IDs)
# ---------------------------------------------------------------------------

class SeenState:
    """
    Persists the most-recently-seen message ID per channel so we never
    re-process old messages across restarts.
    """

    def __init__(self, state_path: Path):
        self._path = state_path
        self._data: dict[str, str] = self._load()

    def _load(self) -> dict:
        if not self._path.exists():
            self._path.parent.mkdir(parents=True, exist_ok=True)
            return {}

        # Guard against empty file (e.g. created by touch or interrupted write)
        raw = self._path.read_text(encoding="utf-8").strip()
        if not raw:
            logger.warning(
                "last_seen_ids.json exists but is empty — starting with no state."
            )
            return {}

        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            logger.error(
                "last_seen_ids.json is corrupted (%s) — starting with no state. "
                "Original file left untouched at: %s",
                e, self._path,
            )
            return {}

    def _save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)
        tmp.replace(self._path)  # atomic rename on POSIX

    def get_last_id(self, channel_name: str) -> Optional[str]:
        return self._data.get(channel_name)

    def set_last_id(self, channel_name: str, msg_id: str) -> None:
        self._data[channel_name] = msg_id
        self._save()


# ---------------------------------------------------------------------------
# Core scanner class
# ---------------------------------------------------------------------------

class DiscordScanner:
    """
    Manages a persistent Playwright browser session and provides
    message-scraping for individual Discord channels.
    """

    def __init__(
        self,
        user_data_dir: str,
        state_path: Path,
        headless: bool = True,
        nav_timeout_ms: int = 30_000,
        msg_load_wait_ms: int = 3_000,
    ):
        self._user_data_dir = user_data_dir
        self._state = SeenState(state_path)
        self._headless = headless
        self._nav_timeout = nav_timeout_ms
        self._msg_wait = msg_load_wait_ms

        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Launch browser and open a persistent context."""
        logger.info("Starting Playwright browser (headless=%s).", self._headless)
        self._playwright = sync_playwright().start()
        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=self._user_data_dir,
            headless=self._headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
            viewport={"width": 1280, "height": 900},
        )
        self._page = self._context.new_page()
        logger.info("Browser context ready.")

    def stop(self) -> None:
        """Gracefully close browser."""
        try:
            if self._context:
                self._context.close()
            if self._playwright:
                self._playwright.stop()
        except Exception as e:
            logger.warning("Error during browser shutdown: %s", e)
        finally:
            self._playwright = None
            self._context = None
            self._page = None
        logger.info("Browser stopped.")

    def ensure_logged_in(self) -> bool:
        """
        Navigate to Discord home and check if we're authenticated.
        Returns True if already logged in, False if login is needed.
        """
        assert self._page, "Call start() first."
        try:
            self._page.goto(
                "https://discord.com/channels/@me",
                timeout=self._nav_timeout,
            )
            self._page.wait_for_timeout(2000)
            # If the login form is present, we're not logged in
            logged_in = not self._page.is_visible(
                "input[name='email']", timeout=3000
            )
            logger.info(
                "Login status: %s",
                "authenticated" if logged_in else "NOT logged in",
            )
            return logged_in
        except PlaywrightTimeout:
            logger.warning("Timeout checking login status.")
            return False

    def login(self, email: str, password: str) -> bool:
        """
        Perform interactive login. Only needed once — the persistent
        profile will store the session for future runs.
        """
        assert self._page, "Call start() first."
        try:
            self._page.goto(
                "https://discord.com/login",
                timeout=self._nav_timeout,
            )
            self._page.wait_for_selector("input[name='email']", timeout=10_000)
            self._page.fill("input[name='email']", email)
            self._page.fill("input[name='password']", password)
            self._page.click("button[type='submit']")
            # Wait for navigation away from login page
            self._page.wait_for_url("**/channels/**", timeout=30_000)
            logger.info("Login successful.")
            return True
        except Exception as e:
            logger.error("Login failed: %s", e)
            return False

    # ------------------------------------------------------------------
    # Message scraping
    # ------------------------------------------------------------------

    def scrape_channel(
        self, channel: dict
    ) -> Generator[dict, None, None]:
        """
        Navigate to a channel URL, scrape visible messages, and yield
        only those not yet seen.

        Yields dicts:
            {
                "msg_id":       "9876543210",
                "channel_name": "4-hour-alert",
                "header_text":  "OverkillAPP — 1:00 AM",
                "body_text":    "XLMUSD is showing a BIG 🟢 on ...",
            }
        """
        assert self._page, "Call start() first."
        channel_name = channel["name"]
        url          = channel["url"]
        last_id      = self._state.get_last_id(channel_name)

        logger.info("Scraping channel: %s", channel_name)
        logger.debug("Navigating to: %s", url)

        try:
            self._page.goto(url, timeout=self._nav_timeout, wait_until="domcontentloaded")
            logger.debug("Page loaded, waiting for Discord to render messages...")
            
            # Wait for the message list container to appear
            try:
                self._page.wait_for_selector(
                    'div[class*="messagesWrapper"]',
                    timeout=10_000,
                    state="visible"
                )
                logger.debug("Message container visible")
            except PlaywrightTimeout:
                logger.debug("Message container not found with messagesWrapper class")
            
            # Give Discord time to render messages
            self._page.wait_for_timeout(self._msg_wait)
            
        except PlaywrightTimeout:
            logger.error(
                "Navigation timeout for channel '%s' (%s).", channel_name, url
            )
            return

        # Try multiple selector strategies
        msg_elements = []
        
        # Strategy 1: Original selector
        try:
            msg_elements = self._page.query_selector_all(_SEL_MSG_GROUP)
            if msg_elements:
                logger.debug("Found %d messages with selector: %s", len(msg_elements), _SEL_MSG_GROUP)
        except Exception as e:
            logger.debug("Selector %s failed: %s", _SEL_MSG_GROUP, e)
        
        # Strategy 2: Alternative list item selector
        if not msg_elements:
            try:
                msg_elements = self._page.query_selector_all("li[id^='chat-messages']")
                if msg_elements:
                    logger.debug("Found %d messages with alternative selector", len(msg_elements))
            except Exception as e:
                logger.debug("Alternative selector failed: %s", e)
        
        # Strategy 3: Any list item in message list
        if not msg_elements:
            try:
                msg_elements = self._page.query_selector_all('ol[data-list-id="chat-messages"] > li')
                if msg_elements:
                    logger.debug("Found %d messages with data-list-id selector", len(msg_elements))
            except Exception as e:
                logger.debug("data-list-id selector failed: %s", e)

        if not msg_elements:
            logger.warning("No messages found with any selector strategy in '%s'.", channel_name)
            return

        logger.debug("Processing %d message elements...", len(msg_elements))

        newest_id_this_cycle: Optional[str] = None
        new_count = 0

        for el in msg_elements:
            try:
                # Get element ID
                element_id = el.get_attribute("id") or el.get_attribute("data-list-item-id") or ""
                
                if not element_id:
                    continue
                
                msg_id = _extract_msg_id(element_id)

                # Skip messages we've already processed (numeric comparison)
                if last_id:
                    try:
                        if int(msg_id) <= int(last_id):
                            continue
                    except ValueError:
                        # Fallback to string comparison if conversion fails
                        if msg_id <= last_id:
                            continue

                # Extract header text (author + timestamp line)
                header_text = ""
                for header_sel in ["h3[class*='header']", "h2[class*='header']", "span[class*='timestamp']"]:
                    try:
                        header_el = el.query_selector(header_sel)
                        if header_el:
                            header_text = header_el.inner_text().strip()
                            break
                    except:
                        continue
                
                if not header_text:
                    logger.debug("No header found for message %s", msg_id)

                # Extract message body
                body_text = ""
                for content_sel in [
                    "div[id^='message-content-']",
                    "div[class*='messageContent']",
                    "div[class*='markup']"
                ]:
                    try:
                        body_el = el.query_selector(content_sel)
                        if body_el:
                            body_text = body_el.inner_text().strip()
                            break
                    except:
                        continue

                # Skip empty messages (embeds, reactions, images, etc.)
                if not body_text:
                    continue

                # Track the newest ID seen this cycle (numeric comparison)
                if newest_id_this_cycle is None:
                    newest_id_this_cycle = msg_id
                else:
                    try:
                        if int(msg_id) > int(newest_id_this_cycle):
                            newest_id_this_cycle = msg_id
                    except ValueError:
                        # Fallback to string comparison
                        if msg_id > newest_id_this_cycle:
                            newest_id_this_cycle = msg_id

                new_count += 1
                yield {
                    "msg_id":       msg_id,
                    "channel_name": channel_name,
                    "header_text":  header_text,
                    "body_text":    body_text,
                }

            except Exception as e:
                logger.warning(
                    "Error processing element in '%s': %s", channel_name, e
                )
                continue

        # Persist the highest ID we saw in this cycle
        if newest_id_this_cycle:
            self._state.set_last_id(channel_name, newest_id_this_cycle)
            logger.info(
                "Channel '%s': yielded %d new messages. Last ID → %s",
                channel_name, new_count, newest_id_this_cycle,
            )
        else:
            logger.info("Channel '%s': no new messages.", channel_name)

    def scrape_all_channels(
        self, channels: list[dict]
    ) -> Generator[dict, None, None]:
        """
        Iterate over all enabled channels and yield new messages.
        Handles per-channel errors without aborting the full cycle.
        """
        for channel in channels:
            if not channel.get("enabled", False):
                logger.debug(
                    "Channel '%s' is disabled, skipping.", channel["name"]
                )
                continue
            try:
                yield from self.scrape_channel(channel)
            except Exception as e:
                logger.error(
                    "Unhandled error scraping channel '%s': %s",
                    channel.get("name", "?"), e,
                )
                continue

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()