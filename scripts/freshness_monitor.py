"""Freshness monitor — the "is the scraper still alive?" alert.

Checks how long ago the freshness worker last re-synced any case
(max last_detail_checked_at). If that's older than FRESHNESS_STALE_HOURS, the
scraper is likely dead / stuck / blocked → send a Telegram alert. No browser, no
app imports (own engine from DATABASE_URL) → safe to run alongside the scraper
on a schedule (cron/launchd).

Anti-spam: alerts ONCE per stale episode (marker file) and sends a single
recovery message when it's fresh again.

Env: DATABASE_URL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
     FRESHNESS_STALE_HOURS (default 2), FRESHNESS_MONITOR_MARKER.
"""
import os
import urllib.parse
import urllib.request
from datetime import datetime

from sqlalchemy import create_engine, text

STALE_HOURS = float(os.environ.get("FRESHNESS_STALE_HOURS", "2"))
MARKER = os.environ.get("FRESHNESS_MONITOR_MARKER", "/tmp/segal_freshness_stale.marker")


def send_telegram(textmsg: str) -> None:
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        print("telegram not configured — skipping send")
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": textmsg}).encode()
    try:
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{tok}/sendMessage", data=data, timeout=12
        )
    except Exception as e:
        print(f"telegram send failed: {e}")


def main() -> int:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set")
        return 2

    engine = create_engine(db_url)
    with engine.connect() as conn:
        last = conn.execute(
            text("SELECT max(last_detail_checked_at) FROM cases WHERE competencia='civil'")
        ).scalar()

    now = datetime.utcnow()
    if last is None:
        age_h, stale = None, True
    else:
        age_h = (now - last).total_seconds() / 3600.0
        stale = age_h > STALE_HOURS

    already_alerted = os.path.exists(MARKER)

    if stale and not already_alerted:
        if age_h is None:
            msg = "🔴 Segal scraper STALE — no sync timestamp found at all."
        else:
            msg = (f"🔴 Segal scraper STALE — no case re-synced in {age_h:.1f}h "
                   f"(threshold {STALE_HOURS}h). Likely dead / stuck / blocked.")
        send_telegram(msg)
        with open(MARKER, "w") as fh:
            fh.write(now.isoformat())
        print("ALERT sent (stale)")
    elif not stale and already_alerted:
        send_telegram(f"🟢 Segal scraper RECOVERED — last sync {age_h:.1f}h ago.")
        os.remove(MARKER)
        print("recovery sent")
    else:
        print(f"ok — last sync {age_h:.1f}h ago (stale={stale})"
              if age_h is not None else "no data yet")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
