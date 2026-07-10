"""
setup_login.py
--------------
Run this ONCE to authenticate Discord in the persistent browser profile.
After this succeeds, main.py will use the saved session automatically.

Usage:
    python src/setup_login.py
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

sys.path.insert(0, str(Path(__file__).parent))
from scanner import DiscordScanner

def main():
    email = os.getenv("DISCORD_EMAIL")
    password = os.getenv("DISCORD_PASSWORD")
    user_data_dir = os.getenv("PLAYWRIGHT_USER_DATA_DIR", "./browser_profile")

    if not email or not password:
        print("ERROR: Set DISCORD_EMAIL and DISCORD_PASSWORD in .env")
        sys.exit(1)

    print(f"Launching browser with profile: {user_data_dir}")
    print("This will open a visible browser window for login.")

    with DiscordScanner(
        user_data_dir=user_data_dir,
        state_path=Path("state/last_seen_ids.json"),
        headless=False,  # Visible so you can handle 2FA if needed
    ) as scanner:

        if scanner.ensure_logged_in():
            print("✓ Already logged in. No action needed.")
            return

        print("Logging in...")
        success = scanner.login(email, password)

        if success:
            print("✓ Login successful. Session saved to profile directory.")
            print("  You can now run main.py with headless=True.")
        else:
            print("✗ Login failed. Check credentials or handle 2FA manually.")
            print("  The browser will stay open for 60s — complete login by hand.")
            import time
            time.sleep(60)

if __name__ == "__main__":
    main()