"""Rotation — process multiple enrolled lawyers SEQUENTIALLY over CDP.

One PJUD session per IP at a time: for each lawyer we launch a fresh real Chrome,
wait for the human to log in (their own segunda clave), persist their cases, close
that Chrome, then move to the next. Semi-attended by design (each lawyer logs in
their turn) — no 2Captcha.

Lawyers come from env LAWYERS="rut1:Name One,rut2:Name Two" (name optional).
Run (dry-run by default — nothing persists):
    LAWYERS="19456852:PABLO ACEVEDO" PYTHONPATH=. <venv> python scripts/rotate_per_abogado.py
Real sync:  ... DRY_RUN=0 ...

Pause Carla (the Clave Única freshness scraper) before running — one PJUD
session per IP at a time.
"""
import asyncio
import os
import shutil
import subprocess

from scripts.per_abogado_launcher import (
    DEBUG_PORT,
    LOGIN_TIMEOUT,
    PROFILE_DIR,
    _launch_chrome,
    _logged_in,
)
from scripts.cdp_scraper_sketch import normalize_rut, persist_via_cdp
from scripts.freshness_monitor import send_telegram  # re-auth/failure alerts


def _parse_lawyers() -> list:
    """env LAWYERS='rut:Name,rut2:Name2' → [(rut, name), ...] (name optional)."""
    pairs = []
    for entry in os.environ.get("LAWYERS", "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" in entry:
            rut, name = entry.split(":", 1)
            pairs.append((rut.strip(), name.strip()))
        else:
            pairs.append((entry, ""))
    return pairs


def _kill_chrome() -> None:
    subprocess.run(
        ["pkill", "-9", "-f", f"remote-debugging-port={DEBUG_PORT}"], capture_output=True
    )


async def main() -> int:
    lawyers = _parse_lawyers()
    if not lawyers:
        print("No lawyers. Set LAWYERS='rut1:Name,rut2:Name'.")
        return 2
    dry = os.environ.get("DRY_RUN", "1").strip() != "0"
    print(f"Rotation: {len(lawyers)} lawyer(s) · dry_run={dry}")

    results = []
    for i, (rut, name) in enumerate(lawyers, 1):
        label = name or rut
        print(f"\n{'#'*62}\n#  [{i}/{len(lawyers)}]  {label}  ({normalize_rut(rut)})\n{'#'*62}")

        # Fresh Chrome + profile for this lawyer (one session per IP at a time).
        _kill_chrome()
        shutil.rmtree(PROFILE_DIR, ignore_errors=True)
        await asyncio.sleep(2)
        _launch_chrome()

        print(f"  👉 Logueá a {label} (Clave del Poder Judicial) en la ventana. Esperando...")
        waited = 0
        while waited < LOGIN_TIMEOUT:
            if _logged_in():
                print("  ✓ login detectado")
                break
            await asyncio.sleep(4)
            waited += 4
        else:
            print(f"  ✗ timeout esperando login — salteo a {label}")
            results.append((label, "skip:login-timeout"))
            _kill_chrome()
            continue

        try:
            # One human login should sync the lawyer's FULL caseload: the scraping
            # itself is continuous activity that keeps the PJUD session alive (no idle
            # timeout while requests flow), so a big batch = fewer human logins.
            batch = int(os.environ.get("LAWYER_BATCH", "200"))
            await persist_via_cdp(rut, name=name, max_pages=2, batch=batch, dry_run=dry, reserved_first=True)
            results.append((label, "ok"))
        except Exception as e:
            print(f"  persist failed for {label}: {type(e).__name__}: {str(e)[:140]}")
            results.append((label, f"error:{type(e).__name__}"))
        finally:
            _kill_chrome()
            await asyncio.sleep(2)

    print(f"\n{'='*62}\n  ROTATION SUMMARY (dry_run={dry})")
    for label, status in results:
        print(f"    • {label}: {status}")
    print("=" * 62)

    # Re-auth / failure signal: tell the operator which lawyers need to re-login
    # and be retried (session expired, login timeout, or scrape error).
    failed = [(lbl, st) for lbl, st in results if st != "ok"]
    if failed:
        body = "\n".join(f"• {lbl}: {st}" for lbl, st in failed)
        send_telegram(
            f"⚠️ Rotación per-abogado: {len(failed)}/{len(results)} requieren atención "
            f"(reconectar + reintentar):\n{body}"
        )
        print(f"  ⚠️ {len(failed)} abogado(s) requieren re-login/reintento — alerta Telegram enviada.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
