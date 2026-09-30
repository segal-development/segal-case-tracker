"""Valida a demanda la clave PJUD de uno o más abogados, sin esperar al worker.

Cuando se carga una clave nueva (``scripts/cargar_clave_interactiva.sh``), la
bóveda queda en "Pendiente de validar" hasta que el scraping la pruebe solo, lo
que puede tardar horas. Este script hace esa prueba al instante: reusa
``app.workers.sync_scheduler._reauth`` (login real headless contra PJUD, que
registra ``validation_ok`` / ``validation_failed`` en ``credential_audit_events``,
lo que lee la bóveda). No hay un camino de login propio.

Efectos de ``_reauth`` que debes conocer: persiste la sesión nueva en el
SessionStore y limpia o fija ``credential_alert_sent_at``. Nunca imprime la clave.

Uso (``PYTHONPATH=.`` es OBLIGATORIO: el proyecto no se instala como paquete en
el venv, así que sin eso no resuelve ``app`` y el script no arranca):
  PYTHONPATH=. poetry run python scripts/validar_clave.py 20217325-K
  PYTHONPATH=. poetry run python scripts/validar_clave.py 20.217.325-k 19586894-8
  PYTHONPATH=. poetry run python scripts/validar_clave.py --forzar 20217325-K

Requisitos: el proxy de Cloud SQL (com.segal.sqlproxy, puerto 5433) y Redis
deben estar arriba, y el entorno (DATABASE_URL, etc.) cargado como en
``scripts/cargar_clave_interactiva.sh``.

Códigos de salida: 0 todas validadas · 1 alguna falló o el RUT no existe ·
2 abortado por la guarda de la estación (no se intentó ningún login).

Guarda de seguridad: si la estación de scraping corre en esta máquina, el script
NO valida. Para pausar SOLO el worker (deja el proxy, que el script necesita):
  UID=$(id -u)
  launchctl disable "gui/$UID/com.segal.syncstation"
  launchctl bootout "gui/$UID/com.segal.syncstation"
y para reanudarlo: ``enable`` primero y luego ``bootstrap`` (ver la nota de
control de la estación). La alternativa es ejecutar el script en la VM.
"""
import argparse
import asyncio
import subprocess
import sys
from typing import Optional

from app.core.database import SessionLocal
from app.models.lawyer import Lawyer
from app.services.session_store import get_session_store
from app.utils.rut import clean_rut
from app.workers.sync_scheduler import _reauth

EXIT_OK = 0
EXIT_FALLO = 1
EXIT_ABORTADO = 2

# Only the WORKER scrapes PJUD. com.segal.sqlproxy is just the DB tunnel, and
# this script needs it, so it must NOT count as "the station is active".
WORKER_LABEL = "com.segal.syncstation"
WORKER_MODULE = "app.workers.sync_scheduler"

MENSAJE_ABORTO = f"""\
ABORTADO: la estación de scraping está activa en esta máquina ({WORKER_LABEL}).

PJUD permite UNA sola sesión activa por IP. Validar aquí le rompería la sesión
a la estación, y además la sesión nueva pisaría la que ella está usando.
No se validó ninguna clave. Tienes dos salidas:

  1. Pausar la estación (solo el worker; el proxy debe seguir arriba) y volver
     a ejecutar este script:
       UID=$(id -u)
       launchctl disable "gui/$UID/{WORKER_LABEL}"
       launchctl bootout "gui/$UID/{WORKER_LABEL}"
  2. Ejecutar este script en la VM.

Solo si sabes que la estación está pausada, usa --forzar para saltear esta guarda.
"""

MENSAJE_SIN_VERIFICAR = (
    "AVISO: no se pudo verificar si la estación de scraping está activa en esta "
    "máquina; se continúa. PJUD permite UNA sola sesión activa por IP: si la "
    "estación corre en esta IP, esta validación le romperá la sesión."
)


def _ejecutar(cmd: list[str]) -> str:
    """Run a read-only probe and return its stdout (raises if it cannot run)."""
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=10, check=True
    ).stdout


def estacion_activa() -> Optional[bool]:
    """Is the scraping station running on THIS machine?

    WHY THIS GUARD EXISTS — do not remove it thinking it is paranoia: PJUD
    allows a single active session per IP (documented in consulta_sync.py,
    cu_rotator.py, per_abogado_launcher.py, map_pjud_tribunales.py and
    spike_auto_token.py, all of which say "pause the station first"). If this
    script logs in while the station runs, it kills the station's session, and
    ``_reauth`` then persists the new session over the one the station uses.

    Returns True (active), False (not running) or None (could not determine:
    not macOS, no launchctl, probe failed). None must NOT block: a guard that
    stops work in an environment it does not understand is worse than none.
    """
    # Signal 1: the LaunchAgent has a live PID (a loaded-but-stopped one shows "-").
    launchctl: Optional[bool] = None
    try:
        for line in _ejecutar(["launchctl", "list"]).splitlines():
            partes = line.split()
            if len(partes) >= 3 and partes[2] == WORKER_LABEL:
                launchctl = partes[0].isdigit()
                break
        else:
            launchctl = False
    except (OSError, subprocess.SubprocessError):
        launchctl = None
    if launchctl:
        return True

    # Signal 2: the worker process itself, e.g. started by hand or by another
    # supervisor. Our own command line never contains the module name.
    proceso: Optional[bool]
    try:
        salida = _ejecutar(["ps", "-axo", "pid=,command="])
        proceso = any(WORKER_MODULE in linea for linea in salida.splitlines())
    except (OSError, subprocess.SubprocessError):
        proceso = None
    if proceso:
        return True

    # "Not running" is only trustworthy if launchctl could answer.
    return False if launchctl is False else None


async def _validar(db, ruts: list[str]) -> bool:
    """Validate each RUT in turn; return True if every one succeeded."""
    store = get_session_store()
    todo_ok = True
    for bruto in ruts:
        rut = clean_rut(bruto)
        lawyer = db.query(Lawyer).filter(Lawyer.rut == rut).first()
        if not lawyer:
            print(f"  {rut}: no hay abogado con ese RUT, se omite")
            todo_ok = False
            continue
        nombre = lawyer.name
        try:
            session, motivo = await _reauth(lawyer, store)
            # _reauth may set/clear credential_alert_sent_at without committing.
            db.commit()
        except Exception as exc:
            session, motivo = None, f"error inesperado: {type(exc).__name__}"
        if session is not None:
            print(f"  {rut} · {nombre}: validada")
        else:
            print(f"  {rut} · {nombre}: falló ({motivo})")
            todo_ok = False
    return todo_ok


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Valida a demanda la clave PJUD de uno o más abogados."
    )
    parser.add_argument("ruts", nargs="+", help="RUT del abogado (con o sin puntos)")
    parser.add_argument(
        "--forzar",
        action="store_true",
        help="saltea la guarda de la estación; solo si sabes que está pausada",
    )
    args = parser.parse_args(argv)

    if args.forzar:
        print("AVISO: --forzar activo, no se verifica la estación de scraping.")
    else:
        estado = estacion_activa()
        if estado is True:
            print(MENSAJE_ABORTO)
            return EXIT_ABORTADO
        if estado is None:
            print(MENSAJE_SIN_VERIFICAR)

    db = SessionLocal()
    try:
        ok = asyncio.run(_validar(db, args.ruts))
    finally:
        db.close()
    return EXIT_OK if ok else EXIT_FALLO


if __name__ == "__main__":
    sys.exit(main())
