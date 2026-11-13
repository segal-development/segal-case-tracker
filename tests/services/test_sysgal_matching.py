"""Matching a Sysgal causa to our Case: ROL + tribunal, fallback ROL + client RUT, never guess."""

import pytest

from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.services.sysgal_matching import match_causa, normalize_rol


@pytest.fixture
def lawyer(db):
    lw = Lawyer(rut="16021492-9", name="Firm")
    db.add(lw)
    db.flush()
    return lw


def _court(db, code, name):
    c = Court(code=code, name=name, region="RM", type="civil")
    db.add(c)
    db.flush()
    return c


def _case(db, lawyer, court, rol="C-100-2026", procedure=None, rut=None):
    case = Case(lawyer_id=lawyer.id, court_id=court.id, rol=rol, procedure=procedure, competencia="civil")
    db.add(case)
    db.flush()
    if rut:
        db.add(CaseLitigante(case_id=case.id, participante="DTE.", rut=rut, persona_type="NATURAL",
                             nombre="X", natural_key=f"{case.id}-{rut}"))
        db.flush()
    return case


class TestNormalizeRol:
    @pytest.mark.parametrize("raw,expected", [
        ("C-100-2026", "C-100-2026"), ("c-100-2026", "C-100-2026"),
        ("100-2026", "C-100-2026"), ("  C-100-2026 ", "C-100-2026"), ("", ""),
    ])
    def test_normalizes(self, raw, expected):
        assert normalize_rol(raw) == expected


class TestRolPlusTribunal:
    def test_matches_with_ordinal_sign_variants(self, db, lawyer):
        c1 = _case(db, lawyer, _court(db, "A", "7º Juzgado Civil de Santiago"))
        _case(db, lawyer, _court(db, "B", "8º Juzgado Civil de Santiago"))

        r = match_causa(db, "C-100-2026", "7°  Juzgado civil de Santiago", None, None)

        assert (r.status, r.case_id, r.method) == ("calzada", c1.id, "rol+tribunal")

    def test_prefers_tribunal_codigo_over_name(self, db, lawyer):
        c1 = _case(db, lawyer, _court(db, "1234", "Juzgado Uno"))
        _case(db, lawyer, _court(db, "9999", "Juzgado Dos"))

        r = match_causa(db, "C-100-2026", "Juzgado Dos", "1234", None)

        assert (r.status, r.case_id) == ("calzada", c1.id)

    def test_bare_rol_from_sysgal_matches(self, db, lawyer):
        c1 = _case(db, lawyer, _court(db, "A", "Juzgado Uno"))
        assert match_causa(db, normalize_rol("100-2026"), "Juzgado Uno", None, None).case_id == c1.id

    def test_same_rol_other_tribunal_without_rut_is_sin_calce(self, db, lawyer):
        _case(db, lawyer, _court(db, "A", "Juzgado Uno"))
        r = match_causa(db, "C-100-2026", "Juzgado Otro", None, None)
        assert (r.status, r.case_id, r.method) == ("sin_calce", None, None)

    def test_unknown_rol_is_sin_calce(self, db, lawyer):
        _case(db, lawyer, _court(db, "A", "Juzgado Uno"))
        assert match_causa(db, "C-999-2026", "Juzgado Uno", None, None).status == "sin_calce"


class TestRolPlusRutFallback:
    def test_tribunal_mismatch_resolved_by_client_rut(self, db, lawyer):
        court_a = _court(db, "A", "Juzgado Uno")
        c1 = _case(db, lawyer, court_a, rut="12345678-5")
        _case(db, lawyer, _court(db, "B", "Juzgado Dos"), rut="99999999-9")

        r = match_causa(db, "C-100-2026", "Nombre Distinto", None, "12.345.678-5")

        assert (r.status, r.case_id, r.method) == ("calzada", c1.id, "rol+rut")

    def test_two_cuadernos_in_same_tribunal_disambiguated_by_rut(self, db, lawyer):
        court = _court(db, "A", "Juzgado Uno")
        _case(db, lawyer, court, procedure="Ejecutivo", rut="11111111-1")
        c2 = _case(db, lawyer, court, procedure="Apremio", rut="12345678-5")

        r = match_causa(db, "C-100-2026", "Juzgado Uno", None, "12345678-5")

        assert (r.status, r.case_id, r.method) == ("calzada", c2.id, "rol+rut")

    def test_ambiguous_when_rut_also_ties(self, db, lawyer):
        court = _court(db, "A", "Juzgado Uno")
        _case(db, lawyer, court, procedure="Ejecutivo", rut="12345678-5")
        _case(db, lawyer, court, procedure="Apremio", rut="12345678-5")

        r = match_causa(db, "C-100-2026", "Juzgado Uno", None, "12345678-5")

        assert (r.status, r.case_id, r.method) == ("ambigua", None, None)

    def test_ambiguous_without_rut(self, db, lawyer):
        court = _court(db, "A", "Juzgado Uno")
        _case(db, lawyer, court, procedure="Ejecutivo")
        _case(db, lawyer, court, procedure="Apremio")
        assert match_causa(db, "C-100-2026", "Juzgado Uno", None, None).status == "ambigua"

    def test_rut_fallback_ambiguous_across_tribunals(self, db, lawyer):
        _case(db, lawyer, _court(db, "A", "Juzgado Uno"), rut="12345678-5")
        _case(db, lawyer, _court(db, "B", "Juzgado Dos"), rut="12345678-5")
        assert match_causa(db, "C-100-2026", "Otro", None, "12345678-5").status == "ambigua"


class TestPrincipalCuaderno:
    """One causa stored as several cuadernos (rows) in the same tribunal: the Principal wins."""

    def test_principal_plus_incidente_same_court_matches_principal(self, db, lawyer):
        court = _court(db, "A", "Juzgado Uno")
        _case(db, lawyer, court, procedure="Administración Concursal")
        principal = _case(db, lawyer, court, procedure="Principal")

        r = match_causa(db, "C-100-2026", "Juzgado Uno", None, None)

        assert (r.status, r.case_id, r.method) == ("calzada", principal.id, "rol+tribunal")

    def test_principal_comparison_is_case_insensitive_and_trimmed(self, db, lawyer):
        court = _court(db, "A", "Juzgado Uno")
        _case(db, lawyer, court, procedure="Incidente Abandono del Proc.")
        principal = _case(db, lawyer, court, procedure="  principal ")

        r = match_causa(db, "C-100-2026", "Juzgado Uno", None, None)

        assert (r.status, r.case_id) == ("calzada", principal.id)

    def test_two_principal_stay_ambiguous(self, db, lawyer):
        court = _court(db, "A", "Juzgado Uno")
        other = Lawyer(rut="22222222-2", name="Other")
        db.add(other)
        db.flush()
        _case(db, lawyer, court, procedure="Principal")
        _case(db, other, court, procedure="Principal")
        _case(db, lawyer, court, procedure="Apremio")

        r = match_causa(db, "C-100-2026", "Juzgado Uno", None, None)

        assert (r.status, r.case_id, r.method) == ("ambigua", None, None)

    def test_no_principal_stays_ambiguous(self, db, lawyer):
        court = _court(db, "A", "Juzgado Uno")
        _case(db, lawyer, court, procedure="Ejecutivo")
        _case(db, lawyer, court, procedure=None)

        assert match_causa(db, "C-100-2026", "Juzgado Uno", None, None).status == "ambigua"

    def test_different_courts_unchanged_even_with_a_principal(self, db, lawyer):
        _case(db, lawyer, _court(db, "A", "Juzgado Uno"), procedure="Principal", rut="12345678-5")
        _case(db, lawyer, _court(db, "B", "Juzgado Dos"), procedure="Apremio", rut="12345678-5")

        r = match_causa(db, "C-100-2026", "Otro", None, "12345678-5")

        assert (r.status, r.case_id) == ("ambigua", None)

    def test_rut_decided_tie_in_same_court_matches_principal_by_rut(self, db, lawyer):
        court = _court(db, "A", "Juzgado Uno")
        _case(db, lawyer, court, procedure="Apremio", rut="12345678-5")
        principal = _case(db, lawyer, court, procedure="Principal", rut="12345678-5")
        other = Lawyer(rut="22222222-2", name="Other")
        db.add(other)
        db.flush()
        _case(db, other, court, procedure="Principal", rut="99999999-9")

        r = match_causa(db, "C-100-2026", "Juzgado Uno", None, "12345678-5")

        assert (r.status, r.case_id, r.method) == ("calzada", principal.id, "rol+rut")

    def test_rut_fallback_without_tribunal_hit_matches_principal_in_single_court(self, db, lawyer):
        court = _court(db, "A", "Juzgado Uno")
        _case(db, lawyer, court, procedure="Apremio", rut="12345678-5")
        principal = _case(db, lawyer, court, procedure="Principal", rut="12345678-5")

        r = match_causa(db, "C-100-2026", "Nombre Distinto", None, "12345678-5")

        assert (r.status, r.case_id, r.method) == ("calzada", principal.id, "rol+rut")


def test_malformed_sysgal_rol_never_matches():
    """Real data (2026-11-09): some Sysgal ROLs are a bare number ('7'). They must stay unmatched."""
    from app.services.sysgal_matching import normalize_rol

    assert normalize_rol("7") == "7"
    assert normalize_rol("1234-2025") == "C-1234-2025"


class TestMatchCausasBulk:
    """``match_causas`` is the bulk twin of ``match_causa``: same decisions, constant queries."""

    def _scenario(self, db, lawyer):
        uno, dos = _court(db, "A", "Juzgado Uno"), _court(db, "B", "Juzgado Dos")
        _case(db, lawyer, uno, rol="C-1-2026")
        _case(db, lawyer, uno, rol="C-2-2026", procedure="Principal")
        _case(db, lawyer, uno, rol="C-2-2026", procedure="Incidente")
        _case(db, lawyer, uno, rol="C-3-2026", procedure="A", rut="11111111-1")
        _case(db, lawyer, dos, rol="C-3-2026", procedure="B", rut="22222222-2")
        _case(db, lawyer, uno, rol="C-4-2026", procedure="A")
        _case(db, lawyer, uno, rol="C-4-2026", procedure="B")
        return [
            ("C-1-2026", "Juzgado Uno", None, None),
            ("C-2-2026", "Juzgado Uno", None, None),
            ("C-3-2026", "Juzgado Tres", None, "11.111.111-1"),
            ("C-3-2026", "Juzgado Tres", None, "9-9"),
            ("C-4-2026", "Juzgado Uno", None, None),
            ("C-9-2026", "Juzgado Uno", None, None),
            ("", "Juzgado Uno", None, None),
        ]

    def test_same_results_as_match_causa(self, db, lawyer):
        from app.services.sysgal_matching import match_causas

        items = self._scenario(db, lawyer)

        assert match_causas(db, items) == [match_causa(db, *item) for item in items]

    def test_empty_input(self, db):
        from app.services.sysgal_matching import match_causas

        assert match_causas(db, []) == []

    def test_query_count_does_not_grow_with_items(self, db, lawyer):
        from sqlalchemy import event

        from app.services.sysgal_matching import match_causas

        items = self._scenario(db, lawyer)
        counts = []
        engine = db.get_bind()
        for batch in (items[:2], items * 5):
            seen = []
            hook = lambda conn, cur, stmt, *a: seen.append(stmt)  # noqa: E731
            event.listen(engine, "before_cursor_execute", hook)
            try:
                match_causas(db, batch)
            finally:
                event.remove(engine, "before_cursor_execute", hook)
            counts.append(len(seen))

        assert counts[1] <= counts[0] + 1
