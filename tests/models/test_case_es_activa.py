"""Guard: nothing in app code may bulk-write terminada_en_sysgal / contrato_estado without es_activa."""

import re
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
# contrato_estado.py is THE recompute: it writes both columns in one Core UPDATE.
ALLOW = {"app/services/contrato_estado.py"}
_BULK = re.compile(r"\bupdate\(\s*(Case\b|Case\.__table__|table\b)|query\(\s*Case\s*\)[^#]*?\.update\(", re.S)
_WINDOW = 700


def bulk_update_violations(source: str) -> list[str]:
    """Snippets that bulk-update Case inputs of es_activa without setting es_activa."""
    found = []
    for match in _BULK.finditer(source):
        window = source[match.start() : match.start() + _WINDOW]
        if ("terminada_en_sysgal" in window or "contrato_estado" in window) and "es_activa" not in window:
            found.append(window.splitlines()[0])
    return found


def test_scanner_flags_a_bulk_update_that_forgets_es_activa():
    assert bulk_update_violations("db.query(Case).filter(x).update({Case.terminada_en_sysgal: True})")
    assert bulk_update_violations("stmt = update(Case).values(contrato_estado='activo')")


def test_scanner_accepts_updates_that_set_es_activa_or_other_columns():
    ok = "update(Case).values(contrato_estado='activo', es_activa=True)"
    assert not bulk_update_violations(ok)
    assert not bulk_update_violations("db.query(Case).filter(x).update({'assigned_at': None})")


def test_no_app_code_bulk_updates_es_activa_inputs_without_es_activa():
    offenders = {}
    for path in APP.rglob("*.py"):
        rel = path.relative_to(APP.parent).as_posix()
        if rel in ALLOW:
            continue
        bad = bulk_update_violations(path.read_text())
        if bad:
            offenders[rel] = bad
    assert offenders == {}
