"""Measure how long a PJUD 2ª-clave session stays alive — to design "login once
per shift". Logs in with the firm's stored 2ª clave + a captcha token YOU provide
(reCAPTCHA is solved by you; we never use 2captcha), then probes the session every
few minutes until it dies, reporting the elapsed lifetime.

Two modes (env KEEP_ALIVE):
  KEEP_ALIVE=1 (default): probe every 3 min — the probe itself keeps the session
                          warm → measures the ABSOLUTE session TTL.
  KEEP_ALIVE=0:           probe every 12 min, idle in between → approximates the
                          INACTIVITY timeout.

Pause Carla before running — one PJUD session per IP. The captcha token is
ephemeral (~2 min) so run this IMMEDIATELY after you generate it.

Run:
  CAPTCHA_TOKEN='<token>' KEEP_ALIVE=1 PYTHONPATH=. <venv> python scripts/measure_pjud_session.py
"""
import asyncio
import os
from datetime import datetime, timezone

CAPTCHA_TOKEN = os.environ.get("CAPTCHA_TOKEN", "")
KEEP_ALIVE = os.environ.get("KEEP_ALIVE", "1") != "0"
HEARTBEAT_MIN = int(os.environ.get("HEARTBEAT_MIN", "3" if KEEP_ALIVE else "12"))
FIRM_RUT = os.environ.get("FIRM_RUT", "16021492-9")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


async def main() -> int:
    if not CAPTCHA_TOKEN:
        print("Set CAPTCHA_TOKEN='<reCAPTCHA token you generated>'. Stopping.")
        return 2

    from app.core.database import SessionLocal
    from app.core.security import decrypt_pjud_password
    from app.models.lawyer import Lawyer
    from app.scrapper.pjud.civil import CivilScraper

    db = SessionLocal()
    law = db.query(Lawyer).filter(Lawyer.rut == FIRM_RUT).first()
    if not law or not law.encrypted_pjud_password:
        print(f"No stored 2ª clave for {FIRM_RUT}. Stopping.")
        return 2
    password = decrypt_pjud_password(law.encrypted_pjud_password)

    sc = CivilScraper(headless=False)
    await sc.start()
    print(f"[{_now()}] logging in with 2ª clave (token)…  keep_alive={KEEP_ALIVE}, probe every {HEARTBEAT_MIN} min")
    session = await sc.login_with_token(FIRM_RUT, password, CAPTCHA_TOKEN)
    t0 = datetime.now(timezone.utc)
    print(f"[{_now()}] LOGIN OK ✓  — measuring session lifetime…")

    n = 0
    try:
        while True:
            await asyncio.sleep(HEARTBEAT_MIN * 60)
            n += 1
            elapsed = (datetime.now(timezone.utc) - t0).total_seconds() / 60
            try:
                cases = await sc.get_my_cases(session=session, max_pages=1)
                ok = cases is not None
                print(f"[{_now()}] probe {n}: {'ALIVE' if ok else 'EMPTY'} · {elapsed:.0f} min · {len(cases) if cases else 0} cases (pg1)")
                if not ok:
                    print(f"[{_now()}] >>> SESSION DIED after ~{elapsed:.0f} min (empty response)")
                    break
            except Exception as e:
                print(f"[{_now()}] >>> SESSION EXPIRED after ~{elapsed:.0f} min · {type(e).__name__}: {str(e)[:80]}")
                break
    finally:
        await sc.stop()
    print(f"\n==== RESULT: 2ª-clave session lasted ~{elapsed:.0f} min (keep_alive={KEEP_ALIVE}) ====")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
