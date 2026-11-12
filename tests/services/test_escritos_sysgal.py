"""Service helpers for Escritos Sysgal: range check and lawyer_id / case_id enrichment."""

from datetime import date, datetime

import pytest

from app.models.lawyer import Lawyer
from app.models.sysgal_causa import SysgalCausa
from app.models.sysgal_usuario import SysgalUsuario
from app.services import escritos_sysgal as svc


class TestCheckRange:
    def test_ok_at_182_days(self):
        svc.check_range(date(2026, 1, 1), date(2026, 7, 2))

    def test_same_day_ok(self):
        svc.check_range(date(2026, 1, 1), date(2026, 1, 1))

    def test_over_182_days(self):
        with pytest.raises(svc.RangeError) as exc:
            svc.check_range(date(2026, 1, 1), date(2026, 7, 3))
        assert str(exc.value) == "El rango máximo es de 6 meses."

    def test_desde_after_hasta(self):
        with pytest.raises(svc.RangeError) as exc:
            svc.check_range(date(2026, 11, 2), date(2026, 11, 1))
        assert str(exc.value) == "La fecha desde debe ser anterior a hasta."


class TestEnrich:
    @pytest.fixture
    def lawyer(self, db):
        lw = Lawyer(rut="22222222-2", name="L", role="lawyer")
        db.add(lw)
        db.commit()
        db.add(SysgalUsuario(sysgal_id=93, nombre="L", lawyer_id=lw.id, synced_at=datetime.utcnow()))
        db.add(SysgalUsuario(sysgal_id=7, nombre="Procurador", lawyer_id=None, synced_at=datetime.utcnow()))
        db.commit()
        return lw

    def test_stats_adds_lawyer_id_and_keeps_numbers(self, db, lawyer):
        stats = {"total": 2, "por_abogado": [{"abogado_id": 93, "nombre": "L", "total": 1},
                                             {"abogado_id": 7, "nombre": "P", "total": 1},
                                             {"abogado_id": 999, "nombre": "?", "total": 0}], "por_tipo": []}
        out = svc.enrich_stats(db, stats)
        assert [(r["abogado_id"], r["total"], r["lawyer_id"]) for r in out["por_abogado"]] == [
            (93, 1, lawyer.id), (7, 1, None), (999, 0, None)]
        assert out["total"] == 2 and out["por_tipo"] == []

    def test_page_adds_lawyer_id_and_case_id_with_one_causa_query(self, db, lawyer):
        from app.models.case import Case

        from app.models.court import Court

        court = Court(code="C1", name="J", region="RM", type="civil")
        db.add(court)
        db.flush()
        case = Case(lawyer_id=lawyer.id, court_id=court.id, rol="C-9061-2026", competencia="civil")
        db.add(case)
        db.flush()

        def causa(sid, case_id):
            return SysgalCausa(sysgal_id=sid, rol="R", rol_norm="r", tribunal="T", case_id=case_id,
                               synced_at=datetime.utcnow())

        db.add_all([causa(115260, case.id), causa(114228, None)])
        db.commit()
        page = {"success": True, "desde": "a", "hasta": "b", "siguiente_desde_id": 5, "data": [
            {"id_escrito": 3, "id_causa": 115260, "abogado_id": 93, "nombre": "L"},
            {"id_escrito": 4, "id_causa": 114228, "abogado_id": 7, "nombre": "P"},
            {"id_escrito": 5, "id_causa": 1, "abogado_id": 93, "nombre": "L"}]}
        statements = []
        from sqlalchemy import event

        def spy(conn, cursor, statement, *a):
            statements.append(statement)

        event.listen(db.get_bind(), "before_cursor_execute", spy)
        try:
            out = svc.enrich_page(db, page)
        finally:
            event.remove(db.get_bind(), "before_cursor_execute", spy)
        assert [(r["id_escrito"], r["lawyer_id"], r["case_id"]) for r in out["data"]] == [
            (3, lawyer.id, case.id), (4, None, None), (5, lawyer.id, None)]
        assert out["siguiente_desde_id"] == 5 and out["desde"] == "a"
        assert sum("sysgal_causas" in q for q in statements) == 1
        assert len(statements) == 2  # usuarios + causas, regardless of the page size

    def test_empty_page_skips_causa_query(self, db, lawyer):
        out = svc.enrich_page(db, {"data": [], "siguiente_desde_id": None})
        assert out["data"] == [] and out["siguiente_desde_id"] is None
