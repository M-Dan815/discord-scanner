"""
notifier.py
-----------
Gmail SMTP email dispatch for matched signals.

Supports two modes:
  - "immediate": one email per signal (fired as soon as the signal is confirmed)
  - "batch":     accumulate signals during a polling cycle, send one digest email
                 at the end

Sender and recipient are separate addresses:
  - gmail_address  — the Gmail account that authenticates + sends (from .env)
  - to_addr        — the recipient (any valid email address, from notification_prefs.json)
"""

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Email formatting helpers
# ---------------------------------------------------------------------------

def _format_signal_row_text(signal: dict) -> str:
    """Single signal as a plain-text block."""
    ts = signal.get("timestamp")
    ts_str = ts.strftime("%Y-%m-%d %H:%M %Z") if ts else "N/A"
    price = signal.get("price")
    price_str = f"{price:.6f}".rstrip("0").rstrip(".") if price else "N/A"

    return (
        f"  Asset:       {signal['base_ticker']} ({signal['asset_type'].upper()})\n"
        f"  Section:     {signal['section'].upper()}\n"
        f"  Signal:      {signal['signal']}\n"
        f"  Timeframe:   {signal['timeframe']}\n"
        f"  Channel:     {signal['channel_name']}\n"
        f"  Price:       {price_str}\n"
        f"  Time:        {ts_str}\n"
    )


def _format_signal_row_html(signal: dict) -> str:
    """Single signal as an HTML table row."""
    ts = signal.get("timestamp")
    ts_str = ts.strftime("%Y-%m-%d %H:%M %Z") if ts else "N/A"

    section_colors = {
        "holdings": "#2ecc71",
        "watchlist": "#f39c12",
        "all": "#95a5a6",
    }
    section_color = section_colors.get(signal["section"], "#000")

    signal_str = signal["signal"]
    signal_color = (
        "#27ae60" if "GREEN"  in signal_str else
        "#e74c3c" if "RED"    in signal_str else
        "#f1c40f" if "GOLDEN" in signal_str else
        "#333"
    )

    return f"""
    <tr>
      <td><strong>{signal['base_ticker']}</strong></td>
      <td>{signal['asset_type'].upper()}</td>
      <td style="color:{section_color}; font-weight:bold;">{signal['section'].upper()}</td>
      <td style="color:{signal_color}; font-weight:bold;">{signal_str}</td>
      <td>{signal['timeframe']}</td>
      <td>{signal['channel_name']}</td>
      <td>{ts_str}</td>
    </tr>
    """



def _build_email(
    signals: list[dict],
    from_addr: str,
    to_addr: str,
    subject_prefix: str,
) -> MIMEMultipart:
    """
    Build a MIME multipart email for one or more signals.

    from_addr — the authenticated Gmail sender address
    to_addr   — the recipient address (separate account)
    """
    count = len(signals)
    subject = (
        f"{subject_prefix} {count} Signal{'s' if count > 1 else ''} Detected"
    )

    # --- Plain text body ---
    text_lines = [
        f"Discord Signal Scanner — {count} match{'es' if count > 1 else ''} found",
        "=" * 60,
        "",
    ]
    for i, sig in enumerate(signals, 1):
        text_lines.append(
            f"[{i}] {sig['base_ticker']} — {sig['signal']} on {sig['timeframe']}"
        )
        text_lines.append(_format_signal_row_text(sig))
        text_lines.append("")

    text_lines.append("=" * 60)
    text_lines.append("Sent by Discord Signal Scanner")
    text_lines.append(f"From: {from_addr}  →  To: {to_addr}")
    text_body = "\n".join(text_lines)

    # --- HTML body ---
    rows_html = "".join(_format_signal_row_html(s) for s in signals)
    html_body = f"""
    <html><body style="font-family: Arial, sans-serif; color: #222;">
      <h2 style="color:#2c3e50;">📡 Discord Signal Scanner</h2>
      <p>{count} signal match{'es' if count > 1 else ''} detected:</p>
      <table border="1" cellpadding="6" cellspacing="0"
             style="border-collapse:collapse; width:100%; font-size:13px;">
        <thead style="background:#2c3e50; color:#fff;">
          <tr>
            <th>Asset</th>
            <th>Type</th>
            <th>Section</th>
            <th>Signal</th>
            <th>Timeframe</th>
            <th>Channel</th>
            <th>Time</th>
          </tr>
        </thead>
        <tbody>
          {rows_html}
        </tbody>
      </table>
      <p style="font-size:11px; color:#888; margin-top:20px;">
        Sent by Discord Signal Scanner &nbsp;|&nbsp;
        From: {from_addr} &nbsp;→&nbsp; To: {to_addr}
      </p>
    </body></html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = to_addr
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    return msg


# ---------------------------------------------------------------------------
# Notifier class
# ---------------------------------------------------------------------------

class Notifier:
    """
    Handles email dispatch for matched signals.

    Authentication always uses the sender Gmail account + app password.
    The recipient (to_addr) is a completely separate email address.
    """

    def __init__(
        self,
        gmail_address: str,       # sender: authenticated Gmail account (from .env)
        gmail_app_password: str,  # sender: Gmail App Password (from .env)
        to_addr: str,             # recipient: separate email address (from notification_prefs.json)
        subject_prefix: str = "[SignalScanner]",
        mode: str = "batch",      # "immediate" or "batch"
    ):
        self._gmail_address = gmail_address
        self._app_password  = gmail_app_password
        self._to            = to_addr
        self._prefix        = subject_prefix
        self._mode          = mode
        self._batch: list[dict] = []

        logger.info(
            "Notifier initialised | sender=%s | recipient=%s | mode=%s",
            self._gmail_address, self._to, self._mode,
        )

    # ------------------------------------------------------------------
    # Internal send
    # ------------------------------------------------------------------

    def _send(self, signals: list[dict]) -> bool:
        """
        Open an SSL connection to Gmail SMTP and send a formatted email.

        The From header and SMTP login both use self._gmail_address.
        The To header uses self._to (the separate recipient account).
        """
        if not signals:
            return True

        msg = _build_email(
            signals,
            from_addr=self._gmail_address,
            to_addr=self._to,
            subject_prefix=self._prefix,
        )

        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
                # Authenticate as the SENDER Gmail account
                smtp.login(self._gmail_address, self._app_password)
                # Deliver to the RECIPIENT address
                smtp.sendmail(
                    from_addr=self._gmail_address,
                    to_addrs=[self._to],
                    msg=msg.as_string(),
                )

            logger.info(
                "Email sent | %d signal(s) | from=%s | to=%s | subject=%s",
                len(signals),
                self._gmail_address,
                self._to,
                msg["Subject"],
            )
            return True

        except smtplib.SMTPAuthenticationError:
            logger.error(
                "SMTP authentication failed for sender account '%s'. "
                "Ensure GMAIL_ADDRESS matches the account the App Password "
                "was generated for, and that the App Password is correct.",
                self._gmail_address,
            )
        except smtplib.SMTPRecipientsRefused as e:
            logger.error(
                "Recipient address '%s' was refused by Gmail SMTP: %s",
                self._to, e,
            )
        except smtplib.SMTPException as e:
            logger.error("SMTP error while sending email: %s", e)
        except Exception as e:
            logger.error("Unexpected error sending email: %s", e)

        return False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_signal(self, signal: dict) -> None:
        """
        Accept a matched signal.
        Immediate mode → send now.
        Batch mode     → accumulate for flush().
        """
        if self._mode == "immediate":
            self._send([signal])
        else:
            self._batch.append(signal)
            logger.debug(
                "Signal batched: %s (%s) | batch size now %d",
                signal["base_ticker"], signal["signal"], len(self._batch),
            )

    def flush(self) -> int:
        """
        Send all accumulated signals in one digest email then clear the batch.
        Returns the number of signals sent (0 if batch was empty or send failed).
        No-op in immediate mode (batch is always empty).
        """
        if not self._batch:
            logger.debug("flush() called — batch empty, nothing to send.")
            return 0

        count = len(self._batch)
        if self._send(self._batch):
            self._batch.clear()
            return count
        else:
            logger.warning(
                "flush() failed — %d signal(s) remain in batch for next attempt.",
                count,
            )
            return 0

    def pending_count(self) -> int:
        """Number of signals waiting in the batch (always 0 in immediate mode)."""
        return len(self._batch)