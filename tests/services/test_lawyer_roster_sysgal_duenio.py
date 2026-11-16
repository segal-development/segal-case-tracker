"""Ownership precedence with Sysgal as primary source (odd/tasks/sysgal-fuente-principal.md S1, S2, S5).

override (assigned_lawyer_id) > active mapped Sysgal lawyer > PJUD litigantes; switch 0 = PJUD only.
"""
from datetime import datetime, timedelta

import pytest

from app.api.deps import resolve_case_scope
from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.sysgal_causa import SysgalCausa
from app.models.sysgal_usuario import SysgalUsuario
from app.services import app_settings
from app.services.lawyer_roster import (
    _abogado_litigantes_by_case,
    acting_lawyer_is_case_abogado,
    case_ids_for_abogado,
    firm_dashboard_stats_all,
    lawyer_dashboard_stats,
    resolve_case_alert_recipients,
    resolved_owner_by_case,
)

T0 = datetime(2026, 11, 1)
_n = iter(range(1, 10_000))


def switch(db, value):
    app_settings.set_int(db, app_settings.KEY_SYSGAL_DUENIO, value)
    db.commit()


@pytest.fixture
def w(db):
    court = Court(code="T-RD", name="Juzgado RD", region="RM", type="civil")
    names = {"pjud": "11111111-1", "edu": "22222222-2", "luis": "33333333-3", "ina": "44444444-4",
             "ovr": "55555555-5", "other": "66666666-6"}
    lawyers = {k: Lawyer(rut=r, name=k.title(), role="lawyer", is_active=(k != "ina")) for k, r in names.items()}
    db.add(court)
    db.add_all(lawyers.values())
    db.flush()
    db.add_all([
        SysgalUsuario(sysgal_id=10, nombre="edu", lawyer_id=lawyers["edu"].id),
        SysgalUsuario(sysgal_id=20, nombre="luis", lawyer_id=lawyers["luis"].id),
        SysgalUsuario(sysgal_id=30, nombre="ina", lawyer_id=lawyers["ina"].id),
        SysgalUsuario(sysgal_id=40, nombre="x", lawyer_id=None),
    ])
    db.commit()
    return {"court": court, **lawyers}


def case(db, w, *, lit=None, sysgal=None, override=None, estado="activa", pendiente=False, owner="pjud",
         eliminada=False):
    n = next(_n)
    c = Case(lawyer_id=w[owner].id, court_id=w["court"].id, rol=f"C-{n}-2026", competencia="civil",
             status="active", patrocinio_pendiente=pendiente,
             assigned_lawyer_id=w[override].id if override else None)
    db.add(c)
    db.flush()
    if lit:
        db.add(CaseLitigante(case_id=c.id, participante="AB.DTE", rut=w[lit].rut, nombre=lit.title(),
                             persona_type="NATURAL", natural_key=f"{c.id}-{lit}"))
    if sysgal is not None:
        db.add(SysgalCausa(sysgal_id=n, rol=c.rol, rol_norm=c.rol, tribunal="J", estado=estado,
                           eliminada=eliminada, abogado_sysgal_id=sysgal, sysgal_updated_at=T0, case_id=c.id))
    db.commit()
    return c


def ruts_by_case(db):
    return {cid: {lit.rut for lit in lits} for cid, lits in _abogado_litigantes_by_case(db).items()}


class TestPrecedence:
    def test_switch_off_is_pjud_only(self, db, w):
        c = case(db, w, lit="pjud", sysgal=10)
        assert ruts_by_case(db)[c.id] == {w["pjud"].rut}
        assert case_ids_for_abogado(db, w["edu"].rut, w["edu"].rut) == set()
        assert resolved_owner_by_case(db)[c.id].id == w["pjud"].id

    def test_sysgal_replaces_pjud_when_on(self, db, w):
        c = case(db, w, lit="pjud", sysgal=10)
        switch(db, 1)
        assert ruts_by_case(db)[c.id] == {w["edu"].rut}
        assert c.id in case_ids_for_abogado(db, w["edu"].rut, w["edu"].rut)
        assert c.id not in case_ids_for_abogado(db, w["pjud"].rut, w["pjud"].rut)
        assert resolved_owner_by_case(db)[c.id].id == w["edu"].id

    def test_sysgal_owner_applies_to_case_without_any_litigante(self, db, w):
        c = case(db, w, sysgal=20)
        switch(db, 1)
        assert resolved_owner_by_case(db)[c.id].id == w["luis"].id

    def test_override_always_wins(self, db, w):
        c = case(db, w, lit="pjud", sysgal=10, override="ovr")
        switch(db, 1)
        assert ruts_by_case(db)[c.id] == {w["ovr"].rut}
        assert resolved_owner_by_case(db)[c.id].id == w["ovr"].id
        assert [r.id for r in resolve_case_alert_recipients(db, c)] == [w["ovr"].id]

    @pytest.mark.parametrize("kw", [
        {"sysgal": 40}, {"sysgal": 30}, {"sysgal": 999}, {"sysgal": 10, "estado": "terminada"},
        {"sysgal": 10, "eliminada": True}, {},
    ])
    def test_unmapped_inactive_terminated_or_absent_fall_through_to_pjud(self, db, w, kw):
        c = case(db, w, lit="pjud", **kw)
        switch(db, 1)
        assert ruts_by_case(db)[c.id] == {w["pjud"].rut}
        assert [r.id for r in resolve_case_alert_recipients(db, c)] == [w["pjud"].id]

    def test_several_rows_latest_wins(self, db, w):
        c = case(db, w, lit="pjud", sysgal=10)
        db.add(SysgalCausa(sysgal_id=9000, rol=c.rol, rol_norm=c.rol, tribunal="J", estado="activa",
                           eliminada=False, abogado_sysgal_id=20, sysgal_updated_at=T0 + timedelta(days=1),
                           case_id=c.id))
        db.commit()
        switch(db, 1)
        assert resolved_owner_by_case(db)[c.id].id == w["luis"].id
        assert [r.id for r in resolve_case_alert_recipients(db, c)] == [w["luis"].id]

    def test_firm_views_follow_the_resolver(self, db, w):
        case(db, w, lit="pjud", sysgal=10)
        before = {r["rut"]: r["case_count"] for r in firm_dashboard_stats_all(db)["by_lawyer"]}
        assert before == {w["pjud"].rut: 1}
        switch(db, 1)
        after = {r["rut"]: r["case_count"] for r in firm_dashboard_stats_all(db)["by_lawyer"]}
        assert after == {w["edu"].rut: 1}
        assert lawyer_dashboard_stats(db, w["edu"].rut)["case_count"] == 1
        assert lawyer_dashboard_stats(db, w["pjud"].rut)["case_count"] == 0


class TestAlertRecipients:
    def test_off_unchanged(self, db, w):
        c = case(db, w, lit="pjud", sysgal=10)
        assert [r.id for r in resolve_case_alert_recipients(db, c)] == [w["pjud"].id]

    def test_on_sysgal_owner_receives_and_pjud_does_not(self, db, w):
        c = case(db, w, lit="pjud", sysgal=10)
        switch(db, 1)
        assert [r.id for r in resolve_case_alert_recipients(db, c)] == [w["edu"].id]

    def test_on_sysgal_owner_beats_patrocinio_pendiente_incorporation_owner(self, db, w):
        c = case(db, w, sysgal=10, pendiente=True, owner="pjud")
        switch(db, 1)
        assert [r.id for r in resolve_case_alert_recipients(db, c)] == [w["edu"].id]

    def test_acting_lawyer_follows_precedence(self, db, w):
        c = case(db, w, lit="pjud", sysgal=10)
        assert acting_lawyer_is_case_abogado(db, c, w["pjud"].rut) is True
        assert acting_lawyer_is_case_abogado(db, c, w["edu"].rut) is False
        switch(db, 1)
        assert acting_lawyer_is_case_abogado(db, c, w["pjud"].rut) is False
        assert acting_lawyer_is_case_abogado(db, c, w["edu"].rut) is True

    def test_acting_lawyer_falls_through_without_sysgal_owner_when_on(self, db, w):
        c = case(db, w, lit="pjud", sysgal=40)
        switch(db, 1)
        assert acting_lawyer_is_case_abogado(db, c, w["pjud"].rut) is True


def _fixture_cases(db, w):
    return [
        case(db, w, lit="pjud"),                                   # PJUD only
        case(db, w, lit="pjud", sysgal=10),                        # sysgal replaces pjud
        case(db, w, lit="pjud", sysgal=10, override="ovr"),        # override wins
        case(db, w, lit="pjud", sysgal=30),                        # inactive sysgal lawyer
        case(db, w, lit="pjud", sysgal=40),                        # unmapped
        case(db, w, lit="pjud", sysgal=20, estado="terminada"),    # terminated
        case(db, w, sysgal=20),                                    # no litigante, sysgal owner
        case(db, w, sysgal=20, pendiente=True),                    # pending patrocinio + sysgal owner
        case(db, w, sysgal=10, pendiente=True, owner="luis"),      # pending, incorporated under another
        case(db, w, sysgal=10, owner="luis"),                      # bootstrap owner differs from sysgal
        case(db, w),                                               # bootstrap, no sysgal
        case(db, w, pendiente=True, owner="other"),                # pending, no sysgal
        case(db, w, lit="other", sysgal=10, owner="luis"),         # litigante is someone else entirely
    ]


@pytest.mark.parametrize("on", [0, 1])
def test_scope_equals_alert_recipients_for_every_case(db, w, on):
    cases = _fixture_cases(db, w)
    switch(db, on)
    lawyers = [w[k] for k in ("pjud", "edu", "luis", "ovr", "other")]
    for c in cases:
        db.refresh(c)
        viewers = {lw.id for lw in lawyers if c.id in resolve_case_scope(db, {"sub": lw.rut})}
        recipients = {r.id for r in resolve_case_alert_recipients(db, c)} or {c.effective_lawyer_id}
        assert viewers == recipients, f"case {c.rol} on={on}"
