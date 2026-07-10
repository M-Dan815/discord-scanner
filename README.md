# Discord Signal Scanner

Monitors Discord trading signal channels and sends Gmail alerts for assets
matching your holdings/watchlist configuration.

---

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your Gmail app password and Discord credentials
```

### 3. First-time Discord login

```bash
python src/setup_login.py
```

This opens a visible browser, logs into Discord, and saves the session.
If you use 2FA, complete it in the browser window — you have 60 seconds.

### 4. Configure channels

Edit `config/channels_config.json`. Set `"enabled": true/false` per channel.
Paste your actual Discord channel URLs in the `"url"` fields.

### 5. Configure tickers

Edit `config/crypto_tickers.json` and `config/stock_tickers.json`.
Add your holdings and watchlist assets. Leave `"all": []` — it auto-populates.

### 6. Configure notification preferences

Edit `config/notification_prefs.json`:

- `notify_sections`: which sections trigger emails (`"holdings"`, `"watchlist"`, `"all"`)
- `notify_timeframes`: which timeframes trigger emails (`"4H"`, `"1D"`, `"1W"`, `"1M"`) or `["ALL"]`
- `email_mode`: `"immediate"` (one email per signal) or `"batch"` (one digest per cycle)
- `email_to`: where to send alerts

### 7. Run the scanner

```bash
python src/main.py
```

Or for continuous operation via cron:

```bash
# Run every minute (the script manages its own internal sleep loop)
@reboot cd /path/to/discord_scanner && python src/main.py >> logs/cron.log 2>&1
```

Or with systemd (recommended for VPS):

```ini
# /etc/systemd/system/discord-scanner.service
[Unit]
Description=Discord Signal Scanner
After=network.target

[Service]
WorkingDirectory=/path/to/discord_scanner
ExecStart=/usr/bin/python3 src/main.py
Restart=on-failure
RestartSec=30
StandardOutput=append:/path/to/discord_scanner/logs/scanner.log
StandardError=append:/path/to/discord_scanner/logs/scanner.log

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable discord-scanner
sudo systemctl start discord-scanner
sudo systemctl status discord-scanner
```

---

## Day-to-Day Operations

### Add a ticker to holdings

Edit `config/crypto_tickers.json` or `config/stock_tickers.json`:

```json
"holdings": ["BTC", "ETH", "SOL", "NEW_TICKER"]
```

The scanner reloads ticker files every 10 cycles — no restart needed.

### Move a ticker from watchlist to holdings

Find it in `"watchlist"`, remove it, add it to `"holdings"`. Save the file.

### Disable a channel

In `config/channels_config.json`, set `"enabled": false` for the channel.

### Add a new channel

Add a new object to the `channels` array in `channels_config.json`:

```json
{
  "name": "new-channel-name",
  "url": "https://discord.com/channels/SERVER_ID/CHANNEL_ID",
  "enabled": true,
  "timeframe": "1D",
  "signal_type": "AUTO"
}
```

### Change notification scope

In `notification_prefs.json`:

```json
"notify_sections": ["holdings"]              // only holdings
"notify_sections": ["holdings", "watchlist"] // holdings + watchlist
"notify_sections": ["holdings", "watchlist", "all"] // everything

"notify_timeframes": ["ALL"]   // all timeframes
"notify_timeframes": ["1D", "1W"] // only daily and weekly
```

### Check what's in 'all'

The `"all"` arrays in both ticker files show every previously-unseen ticker
the scanner auto-detected. Review periodically to promote assets to watchlist
or holdings.

---

## File Reference

| File                             | Purpose                       | Edit manually?                  |
| -------------------------------- | ----------------------------- | ------------------------------- |
| `config/crypto_tickers.json`     | Crypto holdings/watchlist     | Yes (holdings + watchlist only) |
| `config/stock_tickers.json`      | Stock holdings/watchlist      | Yes (holdings + watchlist only) |
| `config/channels_config.json`    | Channel URLs + enable toggles | Yes                             |
| `config/notification_prefs.json` | Email + filter rules          | Yes                             |
| `state/last_seen_ids.json`       | Deduplication state           | No (auto-managed)               |
| `.env`                           | Secrets                       | Yes (once at setup)             |
| `logs/scanner.log`               | Audit trail                   | No (read-only)                  |

---

## Troubleshooting

**"Not logged in" on every startup**  
Re-run `setup_login.py`. The browser profile may have been moved or cleared.

**"SMTP authentication failed"**  
Ensure you're using a Gmail _App Password_ (not your account password).
Generate one at: https://myaccount.google.com/apppasswords

**No messages being scraped**  
Run with `LOG_LEVEL=DEBUG` in `.env` to see raw element counts.
Discord occasionally changes CSS class names — check the selectors
in `scanner.py` against live DOM using browser DevTools.

**Wrong ticker section assignment**  
The ticker may have been added to `"all"` before you put it in `"holdings"`.
Manually move it from `"all"` to `"holdings"` in the ticker file.
The scanner will pick it up on the next ticker reload cycle.
