"""Consumer of the incorporation queue: PJUD is fully mocked (fake fetch)."""

from datetime import datetime, timedelta
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from app.models.case import Case
from app.models.court import Court
from app.models.goal import Goal
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.models.sysgal_causa import SysgalCausa
from app.models.sysgal_incorporacion import SysgalIncorporacion
from app.models.sysgal_usuario import SysgalUsuario
from app.scrapper.pjud.base import PJUDCase, PJUDCaseDetail, PJUDMovement
from app.scrapper.pjud.exceptions import ConsultaSessionExpired, InvalidCredentialsError
from app.services import app_settings
from app.services.sysgal_incorporacion_pjud import run_incorporacion

CHILE = ZoneInfo("America/Santiago")
NOW = datetime(2026, 11, 10, 14, 30, tzinfo=CHILE)


@pytest.fixture
def court(db):
    c = Court(code="C4", name="4º Juzgado Civil de Santiago", region="RM", type="civil",
              pjud_corte=90, pjud_tribunal=4)
    db.add(c)
    db.flush()
    return c


@pytest.fixture
def lawyer(db):
    lw = Lawyer(rut="12345678-5", name="Eduardo Venegas", role="lawyer")
    db.add(lw)
    db.add(SysgalUsuario(sysgal_id=7, nombre="Eduardo", lawyer_id=None))
    db.flush()
    db.query(SysgalUsuario).filter_by(sysgal_id=7).one().lawyer_id = lw.id
    db.flush()
    return lw


def _item(db, court, n, rol=None, prioridad=None, abogado=7, **q):
    rol = rol or f"C-{n}-2026"
    causa = SysgalCausa(sysgal_id=n, rol=rol, rol_norm=rol, tribunal=court.name, abogado_sysgal_id=abogado,
                        synced_at=datetime.utcnow())
    db.add(causa)
    db.flush()
    row = SysgalIncorporacion(sysgal_causa_id=causa.id, estado="pendiente", prioridad=prioridad or n,
                              court_id=court.id, corte=court.pjud_corte, **q)
    db.add(row)
    db.flush()
    return causa, row


def _detail(rol="C-1-2026", movements=1):
    return PJUDCaseDetail(
        case=PJUDCase(rol=rol, tribunal="4º Juzgado Civil de Santiago", caratulado="BANCO / PEREZ",
                      fecha_ingreso="05/01/2026", estado_cuaderno="Tramitación", cuaderno="Principal"),
        movements=[PJUDMovement(folio=str(i + 1), fecha="06/01/2026", tipo_tramite="Escrito", descripcion=f"mov {i}",
                                etapa="Inicio", foja="1", tiene_documento=False) for i in range(movements)],
        procedimiento="Ejecutivo Obligación de Dar",
    )


class Fake:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    async def __call__(self, rol, corte):
        self.calls.append((rol, corte))
        r = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(r, Exception):
            raise r
        return r


async def _run(db, fetch, notify=None, egress_ok=lambda: True, now=NOW):
    notify = notify or MagicMock(return_value=True)
    stats = await run_incorporacion(db, fetch, notify=notify, egress_ok=egress_ok, now=now)
    return stats, notify


def _ritmo(db, dia=100, hora=10):
    app_settings.set_ritmo(db, por_dia=dia, por_hora=hora)
    db.commit()


@pytest.mark.asyncio
async def test_off_by_default_does_nothing(db, court, lawyer):
    _item(db, court, 1)
    db.commit()
    fetch = Fake(_detail())

    await _run(db, fetch)

    assert fetch.calls == []


@pytest.mark.asyncio
async def test_hour_quota_limits_and_priority_order_is_respected(db, court, lawyer):
    _ritmo(db, dia=100, hora=2)
    _item(db, court, 1, prioridad=30)
    _item(db, court, 2, prioridad=10)
    _item(db, court, 3, prioridad=20)
    db.commit()
    fetch = Fake(None)

    await _run(db, fetch)
    assert [c[0] for c in fetch.calls] == ["C-2-2026", "C-3-2026"]

    # Same clock hour: quota already consumed, nothing more goes out.
    fetch2 = Fake(None)
    await _run(db, fetch2)
    assert fetch2.calls == []

    # Next hour: the remaining one goes.
    await _run(db, fetch2, now=NOW + timedelta(hours=1))
    assert [c[0] for c in fetch2.calls] == ["C-1-2026"]


@pytest.mark.asyncio
async def test_day_quota_caps_below_the_hour_quota(db, court, lawyer):
    _ritmo(db, dia=1, hora=10)
    _item(db, court, 1)
    _item(db, court, 2)
    db.commit()
    fetch = Fake(None)

    await _run(db, fetch)
    await _run(db, fetch, now=NOW + timedelta(hours=2))

    assert len(fetch.calls) == 1
    await _run(db, fetch, now=NOW + timedelta(days=1))
    assert len(fetch.calls) == 2


@pytest.mark.asyncio
async def test_egress_not_allowed_makes_no_pjud_request(db, court, lawyer):
    _ritmo(db)
    _item(db, court, 1)
    db.commit()
    fetch = Fake(_detail())

    await _run(db, fetch, egress_ok=lambda: False)

    assert fetch.calls == []
    assert db.query(SysgalIncorporacion).one().estado == "pendiente"


@pytest.mark.asyncio
async def test_found_creates_case_with_lawyer_flag_movements_and_links(db, court, lawyer):
    _ritmo(db)
    causa, row = _item(db, court, 1)
    db.commit()
    fetch = Fake(_detail("C-1-2026", movements=2))

    stats, _ = await _run(db, fetch)

    case = db.query(Case).one()
    assert (case.rol, case.court_id, case.lawyer_id, case.patrocinio_pendiente) == (
        "C-1-2026", court.id, lawyer.id, True)
    assert (case.plaintiff, case.defendant, case.competencia) == ("BANCO", "PEREZ", "civil")
    assert db.query(Movement).filter_by(case_id=case.id).count() == 2
    db.refresh(row)
    db.refresh(causa)
    assert (row.estado, row.case_id, row.intentos) == ("incorporada", case.id, 0)
    assert row.ultimo_intento_at is not None
    assert (causa.case_id, causa.match_method) == (case.id, "pjud_rol")
    assert stats["incorporadas"] == 1


@pytest.mark.asyncio
async def test_found_recomputes_deadlines_for_the_new_case(db, court, lawyer, monkeypatch):
    from app.services import sysgal_incorporacion_pjud as mod

    calls = []
    monkeypatch.setattr(mod, "_maybe_recompute_deadlines", lambda db_, case, **k: calls.append(case.rol))
    _ritmo(db)
    _item(db, court, 1)
    db.commit()

    await _run(db, Fake(_detail()))

    assert calls == ["C-1-2026"]


@pytest.mark.asyncio
async def test_existing_case_same_rol_and_court_is_linked_not_duplicated(db, court, lawyer):
    other = Lawyer(rut="99999999-9", name="Otro", role="lawyer")
    db.add(other)
    db.flush()
    existing = Case(lawyer_id=other.id, court_id=court.id, rol="C-1-2026", competencia="civil",
                    procedure="Principal")
    db.add(existing)
    _ritmo(db)
    causa, row = _item(db, court, 1)
    db.commit()
    fetch = Fake(_detail())

    await _run(db, fetch)

    assert fetch.calls == []  # no PJUD request needed
    assert db.query(Case).count() == 1
    db.refresh(row)
    db.refresh(causa)
    assert (row.estado, row.case_id, causa.case_id, causa.match_method) == (
        "incorporada", existing.id, existing.id, "pjud_rol")
    assert existing.patrocinio_pendiente is False


@pytest.mark.asyncio
async def test_unmapped_lawyer_is_error_without_case_nor_pjud_request(db, court, lawyer):
    _ritmo(db)
    _, row = _item(db, court, 1, abogado=999)
    db.commit()
    fetch = Fake(_detail())

    await _run(db, fetch)

    assert fetch.calls == []
    assert db.query(Case).count() == 0
    db.refresh(row)
    assert (row.estado, row.ultimo_error) == ("error", "abogado sin relacionar")


@pytest.mark.asyncio
async def test_not_found_is_no_visible(db, court, lawyer):
    _ritmo(db)
    _, row = _item(db, court, 1)
    db.commit()

    await _run(db, Fake(None))

    db.refresh(row)
    assert (row.estado, row.case_id) == ("no_visible", None)
    assert db.query(Case).count() == 0


@pytest.mark.asyncio
async def test_failure_backs_off_1h_6h_24h_then_error_after_three(db, court, lawyer):
    _ritmo(db, dia=100, hora=100)
    _, row = _item(db, court, 1)
    db.commit()
    fetch = Fake(RuntimeError("boom <html>secret body</html>"))

    t = NOW
    for attempt, wait in enumerate([timedelta(hours=1), timedelta(hours=6), timedelta(hours=24)], start=1):
        await _run(db, fetch, now=t)
        db.refresh(row)
        assert row.intentos == attempt
        assert "secret" not in (row.ultimo_error or "") and len(row.ultimo_error) <= 255
        if attempt < 3:
            assert row.estado == "pendiente"
            assert row.proximo_intento_at == t.astimezone(ZoneInfo("UTC")).replace(tzinfo=None) + wait
            # Not due yet: nothing goes out.
            n = len(fetch.calls)
            await _run(db, fetch, now=t + wait - timedelta(minutes=1))
            assert len(fetch.calls) == n
            t = t + wait
    assert row.estado == "error"
    assert len(fetch.calls) == 3


@pytest.mark.asyncio
async def test_three_consecutive_same_kind_failures_brake_with_one_telegram(db, court, lawyer):
    _ritmo(db, dia=100, hora=100)
    for n in range(1, 6):
        _item(db, court, n)
    db.commit()
    fetch = Fake(ConsultaSessionExpired("x"))

    stats, notify = await _run(db, fetch)

    assert len(fetch.calls) == 3
    assert stats["frenada"] is True
    notify.assert_called_once_with(
        "Incorporación desde Sysgal detenida: sesión de PJUD expirada. Se reintenta mañana.")
    # Same Chile day, later hour: stays stopped, no second Telegram, no request.
    stats2, notify2 = await _run(db, fetch, now=NOW + timedelta(hours=3))
    assert len(fetch.calls) == 3
    notify2.assert_not_called()
    # Next Chile day it resumes.
    await _run(db, fetch, now=NOW + timedelta(days=1))
    assert len(fetch.calls) > 3


@pytest.mark.asyncio
async def test_different_kinds_or_a_success_reset_the_failure_streak(db, court, lawyer):
    _ritmo(db, dia=100, hora=100)
    for n in range(1, 6):
        _item(db, court, n)
    db.commit()
    fetch = Fake(ConsultaSessionExpired("x"), ConsultaSessionExpired("x"), None, ConsultaSessionExpired("x"),
                 ConsultaSessionExpired("x"))

    stats, notify = await _run(db, fetch)

    assert len(fetch.calls) == 5
    assert stats["frenada"] is False
    notify.assert_not_called()


@pytest.mark.asyncio
async def test_login_rejection_brakes_immediately(db, court, lawyer):
    _ritmo(db, dia=100, hora=100)
    _item(db, court, 1)
    _item(db, court, 2)
    db.commit()
    fetch = Fake(InvalidCredentialsError("bad pass hunter2"))

    stats, notify = await _run(db, fetch)

    assert len(fetch.calls) == 1
    notify.assert_called_once()
    assert "hunter2" not in notify.call_args[0][0]
    assert notify.call_args[0][0].startswith("Incorporación desde Sysgal detenida: ")


@pytest.mark.asyncio
async def test_empty_detail_counts_as_failure_kind_and_is_retried(db, court, lawyer):
    _ritmo(db, dia=100, hora=100)
    _, row = _item(db, court, 1)
    db.commit()

    await _run(db, Fake(_detail(movements=0)))

    db.refresh(row)
    assert (row.estado, row.intentos, row.ultimo_error) == ("pendiente", 1, "detalle vacío")
    assert db.query(Case).count() == 0


@pytest.mark.asyncio
async def test_brake_state_is_stored_in_the_db_so_a_restart_keeps_it(db, court, lawyer):
    _ritmo(db, dia=100, hora=100)
    for n in range(1, 4):
        _item(db, court, n)
    db.commit()
    await _run(db, Fake(ConsultaSessionExpired("x")))

    assert app_settings.get_int(db, app_settings.KEY_FRENO_DIA, 0) == 20261110
    assert db.query(Goal).filter_by(key=app_settings.KEY_FRENO_DIA).count() == 1


def _alerts(db):
    from app.models.alert import Alert

    return db.query(Alert).all()


def _later(folio, desc="nuevo escrito"):
    from app.services.sync_service import convert_api_movements_to_scraped

    return convert_api_movements_to_scraped([{
        "folio": folio, "fecha": "20/10/2026", "tipo_tramite": "Escrito", "descripcion": desc,
        "etapa": "Prueba", "foja": "9", "tiene_documento": False,
    }])


@pytest.mark.asyncio
async def test_initial_load_of_historic_movements_creates_no_alerts(db, court, lawyer):
    _ritmo(db)
    _item(db, court, 1)
    db.commit()

    await _run(db, Fake(_detail("C-1-2026", movements=5)))

    case = db.query(Case).one()
    assert db.query(Movement).filter_by(case_id=case.id).count() == 5
    assert _alerts(db) == []


@pytest.mark.asyncio
async def test_initial_load_sends_no_movement_notification(db, court, lawyer, monkeypatch):
    from app.services.notification_service import NotificationService

    sent = MagicMock()
    monkeypatch.setattr(NotificationService, "notify_new_movement", sent)
    monkeypatch.setattr(NotificationService, "accumulate_movement", sent)
    _ritmo(db)
    _item(db, court, 1)
    db.commit()

    await _run(db, Fake(_detail("C-1-2026", movements=3)))

    sent.assert_not_called()


@pytest.mark.asyncio
async def test_a_later_sync_still_alerts_the_lawyer_for_a_new_movement(db, court, lawyer):
    from app.services.sync_service import SyncService

    _ritmo(db)
    _item(db, court, 1)
    db.commit()
    await _run(db, Fake(_detail("C-1-2026", movements=2)))
    case = db.query(Case).one()

    new, alerts = SyncService(db).sync_movements(case.id, _later("99"))

    assert (new, alerts) == (1, 1)
    [alert] = _alerts(db)
    assert (alert.lawyer_id, alert.case_id, alert.type) == (lawyer.id, case.id, "new_movement")


def test_normal_sync_movements_path_still_alerts_by_default(db, court, lawyer):
    from app.services.sync_service import SyncService

    case = Case(lawyer_id=lawyer.id, court_id=court.id, rol="C-7-2026", competencia="civil")
    db.add(case)
    db.commit()

    new, alerts = SyncService(db).sync_movements(case.id, _later("1"))

    assert (new, alerts) == (1, 1)
    assert [a.lawyer_id for a in _alerts(db)] == [lawyer.id]
