"""GET /sysgal/incorporaciones/resumen — admin only, read-only queue summary."""

from datetime import datetime, timedelta

import pytest

from app.core.security import create_access_token
from app.models.lawyer import Lawyer
from app.models.sysgal_causa import SysgalCausa
from app.models.sysgal_incorporacion import SysgalIncorporacion
from app.models.sysgal_usuario import SysgalUsuario

ADMIN_RUT, LAWYER_RUT = "11111111-1", "22222222-2"
URL = "/api/v1/sysgal/incorporaciones/resumen"


def _headers(rut):
    return {"Authorization": "Bearer " + create_access_token({"sub": rut}, expires_delta=timedelta(minutes=30))}


@pytest.fixture
def people(db):
    db.add_all([Lawyer(rut=ADMIN_RUT, name="Admin", role="admin"),
                Lawyer(rut=LAWYER_RUT, name="Lawyer", role="lawyer")])
    db.commit()


def _queue(db, sid, rol, estado="pendiente", prioridad=0, abogado=None, tribunal="Juzgado Uno"):
    causa = SysgalCausa(sysgal_id=sid, rol=rol, rol_norm=rol, tribunal=tribunal, abogado_sysgal_id=abogado,
                        abogado_nombre="Nombre Sysgal" if abogado else None, synced_at=datetime.utcnow())
    db.add(causa)
    db.flush()
    db.add(SysgalIncorporacion(sysgal_causa_id=causa.id, estado=estado, prioridad=prioridad))
    db.flush()


def test_requires_admin(client, people):
    assert client.get(URL).status_code in (401, 403)
    assert client.get(URL, headers=_headers(LAWYER_RUT)).status_code == 403


def test_summary_counts_and_first_pending_in_priority_order(client, db, people):
    lw = Lawyer(rut="33333333-3", name="Ana Mapeada", role="lawyer")
    db.add(lw)
    db.flush()
    db.add(SysgalUsuario(sysgal_id=7, nombre="Ana Sysgal", lawyer_id=lw.id))
    _queue(db, 1, "C-1-2026", prioridad=2, abogado=7)
    _queue(db, 2, "C-2-2026", prioridad=1, abogado=7)
    _queue(db, 3, "C-3-2025", prioridad=3, abogado=8)
    _queue(db, 4, "C-4-2025", estado="sin_corte")
    for i in range(30):
        _queue(db, 100 + i, f"C-{100 + i}-2024", prioridad=10 + i)
    db.commit()
    before = db.query(SysgalIncorporacion).count()

    body = client.get(URL, headers=_headers(ADMIN_RUT)).json()

    assert body["por_estado"] == {"pendiente": 33, "sin_corte": 1}
    assert body["por_anio"] == {"2026": 2, "2025": 2, "2024": 30}
    assert body["por_abogado"]["Ana Mapeada"] == 2
    assert body["por_abogado"]["Nombre Sysgal"] == 1
    assert len(body["primeras_pendientes"]) == 20
    assert [p["rol"] for p in body["primeras_pendientes"][:3]] == ["C-2-2026", "C-1-2026", "C-3-2025"]
    assert body["primeras_pendientes"][0] == {
        "rol": "C-2-2026", "tribunal": "Juzgado Uno", "abogado": "Ana Mapeada", "anio": 2026}
    assert db.query(SysgalIncorporacion).count() == before
