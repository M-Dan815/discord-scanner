"""
setup_login.py
--------------
Run this ONCE to authenticate Discord in the persistent browser profile.
After this succeeds, main.py will use the saved session automatically.

Usage:
    python src/setup_login.py

If the session has expired and "already logged in" keeps appearing,
run with --force to wipe the old profile and start fresh:
    python src/setup_login.py --force
"""

import os
import sys
import time
import shutil
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

sys.path.insert(0, str(Path(__file__).parent))
from playwright.sync_api import sync_playwright

def wipe_profile(user_data_dir: str) -> None:
    """Delete the existing browser profile so we start completely fresh."""
    profile_path = Path(user_data_dir)
    if profile_path.exists():
        print(f"Wiping old browser profile: {profile_path}")
        shutil.rmtree(profile_path)
        print("✓ Old profile deleted.")
    profile_path.mkdir(parents=True, exist_ok=True)


def check_logged_in(page) -> bool:
    """
    Navigate to Discord and check if we're actually authenticated.
    Waits long enough for redirects to settle.
    """
    page.goto("https://discord.com/login", timeout=30_000)
    time.sleep(3)

    current_url = page.url
    print(f"  URL after navigation: {current_url}")

    # If we got redirected away from /login we're authenticated
    if "/login" not in current_url:
        return True

    # Still on login page — check if the email field is visible
    try:
        page.wait_for_selector("input[name='email']", timeout=5_000)
        return False  # Login form is visible — not authenticated
    except:
        # No login form but still on /login URL — ambiguous, treat as not logged in
        return False


def manual_login(page, email: str, password: str) -> bool:
    """
    Fill in credentials and submit. Waits for redirect away from login page.
    """
    try:
        page.goto("https://discord.com/login", timeout=30_000)
        page.wait_for_selector("input[name='email']", timeout=10_000)

        print("  Filling in credentials...")
        page.fill("input[name='email']", email)
        time.sleep(0.5)
        page.fill("input[name='password']", password)
        time.sleep(0.5)
        page.click("button[type='submit']")

        print("  Waiting for Discord to load after login...")
        # Wait up to 60s for redirect away from login (handles 2FA delay too)
        page.wait_for_url("**/channels/**", timeout=60_000)
        return True

    except Exception as e:
        print(f"  Login attempt failed: {e}")
        return False


def main():
    force = "--force" in sys.argv

    email = os.getenv("DISCORD_EMAIL")
    password = os.getenv("DISCORD_PASSWORD")
    user_data_dir = os.getenv("PLAYWRIGHT_USER_DATA_DIR", "./browser_profile")

    if not email or not password:
        print("ERROR: Set DISCORD_EMAIL and DISCORD_PASSWORD in .env")
        sys.exit(1)

    if force:
        print("--force flag detected: wiping old browser profile.")
        wipe_profile(user_data_dir)
    
    print(f"\nLaunching browser with profile: {user_data_dir}")
    print("A visible browser window will open.\n")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()

        # ------------------------------------------------------------------
        # Step 1: Check if already logged in
        # ------------------------------------------------------------------
        print("Step 1: Checking login status...")
        logged_in = check_logged_in(page)

        if logged_in:
            print("✓ Already logged in and session is valid.")
            print("  If you believe the session is broken, re-run with --force:")
            print("  python src/setup_login.py --force")
            input("\nPress Enter to close browser...")
            context.close()
            return

        print("✗ Not logged in.\n")

        # ------------------------------------------------------------------
        # Step 2: Try automatic login with credentials from .env
        # ------------------------------------------------------------------
        print("Step 2: Attempting automatic login...")
        success = manual_login(page, email, password)

        if success:
            print("\n✓ Login successful! Session saved to profile directory.")
            print("  You can now run main.py with headless=True.")
            time.sleep(2)
            context.close()
            return

        # ------------------------------------------------------------------
        # Step 3: Automatic login failed — wait for manual completion
        # (handles 2FA, CAPTCHA, email verification, etc.)
        # ------------------------------------------------------------------
        print("\n✗ Automatic login failed (possibly 2FA or CAPTCHA).")
        print("=" * 55)
        print("ACTION REQUIRED:")
        print("  Complete the login manually in the browser window.")
        print("  You have 120 seconds.")
        print("=" * 55)

        # Wait for manual completion — poll every 2s for up to 120s
        deadline = time.time() + 120
        verified = False
        while time.time() < deadline:
            current_url = page.url
            if "/channels/" in current_url:
                verified = True
                break
            remaining = int(deadline - time.time())
            print(f"\r  Waiting for login... {remaining}s remaining", end="", flush=True)
            time.sleep(2)

        print()  # newline after the countdown

        if verified:
            print("\n✓ Login detected! Session saved to profile directory.")
            print("  You can now run main.py with headless=True.")
            time.sleep(2)
        else:
            print("\n✗ Timed out waiting for login.")
            print("  Re-run this script and complete login faster,")
            print("  or check your Discord credentials in .env.")

        context.close()


if __name__ == "__main__":
    main()