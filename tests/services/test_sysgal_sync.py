"""Tests for ``sync_sysgal_estados`` — batch RUT lookup + cache upsert."""

from datetime import date, datetime, timedelta

import pytest

from app.config import settings
from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.cliente_sysgal_estado import ClienteSysgalEstado
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.services.sysgal_client import SysgalError
from app.services.sysgal_sync import sync_sysgal_estados
from app.utils.rut import clean_rut


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def lawyer(db):
    obj = Lawyer(rut="11111111-1", name="Sync Lawyer")
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


@pytest.fixture
def court(db):
    obj = Court(code="T1-SY", name="Juzgado SY", region="RM", type="civil")
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


def _make_case(db, lawyer, court, rol, **flags):
    obj = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol=rol,
        status="active",
        competencia="civil",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        **flags,
    )
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


def _add_litigante(db, case_id, participante, rut):
    lit = CaseLitigante(
        case_id=case_id,
        participante=participante,
        rut=rut,
        persona_type="NATURAL",
        nombre="Test",
        natural_key=f"{case_id}-{participante}-{rut}",
    )
    db.add(lit); db.commit()


def _found(code="ACTIVO", label="Activo", hasta="2027-01-31", updated="2026-08-01 12:34:56.123456"):
    return {
        "encontrado": True,
        "id_cliente": 1,
        "rut": "123456789",
        "nombre": "PII",
        "email": "pii@example.test",
        "telefono": "+569",
        "estado_comercial": label,
        "estado_comercial_codigo": code,
        "estado_comercial_color": "#000",
        "tiene_contrato": hasta is not None,
        "contrato": (
            {"id": 1, "vigencia_desde": "2026-01-01", "vigencia_hasta": hasta, "fecha_creacion": "2026-01-01 00:00:00"}
            if hasta
            else None
        ),
        "updated_at": updated,
    }


NOT_FOUND = {"encontrado": False, "estado_comercial": None, "mensaje": "Cliente no encontrado"}


class FakeClient:
    """In-memory stand-in for SysgalClient: records batches, answers per rut."""

    def __init__(self, answers=None, configured=True, fail_batches=()):
        self.answers = answers or {}
        self.configured = configured
        self.fail_batches = set(fail_batches)
        self.batches: list[list[str]] = []

    @property
    def is_configured(self):
        return self.configured

    def estado_por_ruts(self, ruts):
        self.batches.append(list(ruts))
        if len(ruts) > 100:
            raise ValueError("too many")
        if (len(self.batches) - 1) in self.fail_batches:
            raise SysgalError("simulated failure")
        # Like the real API: accepts dotted or canonical RUTs, keys the answer
        # exactly as sent.
        return {r: self.answers.get(clean_rut(r), NOT_FOUND) for r in ruts}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestUnconfigured:
    def test_unconfigured_client_is_noop(self, db):
        result = sync_sysgal_estados(db, client=FakeClient(configured=False))
        assert result["skipped"] is True
        assert result["consultados"] == 0
        assert db.query(ClienteSysgalEstado).count() == 0

    def test_default_client_from_empty_settings_is_noop(self, db, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "SYSGAL_BASE_URL", "")
        monkeypatch.setattr(settings, "SYSGAL_API_KEY", "")
        result = sync_sysgal_estados(db)
        assert result["skipped"] is True


class TestRutSelection:
    def test_selects_parties_whatever_the_case_state(self, db, lawyer, court):
        """Scope is the WHOLE portfolio, not only the 3 states.

        The prune decides whether a causa keeps being detail-scraped at all,
        so it needs live coverage of every demandado — not only of those that
        already reached abandono/apremio/prescripción.
        """
        abandono = _make_case(db, lawyer, court, "C-1-2025", abandono_disponible=True)
        apremio = _make_case(db, lawyer, court, "C-2-2025", en_apremio=True)
        presc = _make_case(db, lawyer, court, "C-3-2025", prescripcion_cumplida=True)
        plain = _make_case(db, lawyer, court, "C-4-2025")

        _add_litigante(db, abandono.id, "DDO.", "12.345.678-9")
        _add_litigante(db, abandono.id, "DTE.", "99999999-9")      # demandante — a party too
        _add_litigante(db, abandono.id, "AB.DDO", "88888888-8")    # demandado's lawyer — excluded
        _add_litigante(db, abandono.id, "AP.DDO", "87777777-7")    # excluded
        _add_litigante(db, apremio.id, "DDOR.", "23456789-0")
        _add_litigante(db, presc.id, "DDO.", "")                    # empty rut — excluded
        _add_litigante(db, presc.id, "DDO.", "12345678-9")          # dup of first (normalized)
        _add_litigante(db, plain.id, "DDO.", "77777777-7")          # no state flag — included

        client = FakeClient()
        result = sync_sysgal_estados(db, client=client)

        assert result["skipped"] is False
        sent = sorted(r for batch in client.batches for r in batch)
        # A valid RUT travels dotted, an invalid one canonical — existing
        # contract of the service, unchanged by the widened scope.
        assert sent == ["12345678-9", "23456789-0", "77.777.777-7", "99.999.999-9"]
        assert result["consultados"] == 4

    def test_case_with_only_representatives_contributes_nothing(self, db, lawyer, court):
        case = _make_case(db, lawyer, court, "C-5-2025")
        _add_litigante(db, case.id, "AB.DTE", "99999999-9")
        client = FakeClient()
        result = sync_sysgal_estados(db, client=client)
        assert client.batches == []
        assert result["consultados"] == 0


class TestPartyScope:
    """The client is not reliably the demandado.

    Measured on the portfolio: where the demandado is a company the demandante
    is a natural person 99.2% of the time, and where the demandado is a natural
    person the demandante is a company 99.6% of the time. Those two populations
    are mirror images, so the litigante roles are inverted on roughly a quarter
    of the causas. Coverage therefore has to be asked for every party and the
    client identified by who Sysgal recognises, never by the role label.
    """

    def test_includes_the_demandante_and_the_other_parties(self, db, lawyer, court):
        case = _make_case(db, lawyer, court, "C-1-2025")
        _add_litigante(db, case.id, "DDO.", "12345678-9")
        _add_litigante(db, case.id, "DTE.", "23456789-0")
        _add_litigante(db, case.id, "DDOR.", "34567890-1")
        _add_litigante(db, case.id, "TER.EJ", "45678901-2")
        _add_litigante(db, case.id, "ACRDOR", "56789012-3")

        client = FakeClient()
        result = sync_sysgal_estados(db, client=client)

        assert result["consultados"] == 5

    def test_excludes_lawyers_and_apoderados(self, db, lawyer, court):
        """A representative is never the client, whatever the role spelling."""
        case = _make_case(db, lawyer, court, "C-1-2025")
        _add_litigante(db, case.id, "DDO.", "12345678-9")
        _add_litigante(db, case.id, "AB.DDO", "88888888-8")
        _add_litigante(db, case.id, "AB.DTE", "88888888-7")
        _add_litigante(db, case.id, "AB.TER", "88888888-6")
        _add_litigante(db, case.id, "ABG.AD", "88888888-5")
        _add_litigante(db, case.id, "AP.DTE", "87777777-7")
        _add_litigante(db, case.id, "AP.DDO", "87777777-6")

        client = FakeClient()
        result = sync_sysgal_estados(db, client=client)

        sent = sorted(r for batch in client.batches for r in batch)
        assert sent == ["12345678-9"]
        assert result["consultados"] == 1

    def test_an_unknown_role_is_looked_up_rather_than_dropped(self, db, lawyer, court):
        """Missing the client costs far more than looking up a perito."""
        case = _make_case(db, lawyer, court, "C-1-2025")
        _add_litigante(db, case.id, "ROL.NUEVO", "12345678-9")

        client = FakeClient()
        result = sync_sysgal_estados(db, client=client)

        assert result["consultados"] == 1


class TestCacheFreshness:
    """Widening the scope must not multiply the per-cycle load on Sysgal.

    A RUT already answered recently is not asked again: the first run fetches
    the backlog, later runs only refresh what went stale.
    """

    def _cached(self, db, rut, days_ago):
        row = ClienteSysgalEstado(
            rut=clean_rut(rut),
            encontrado=True,
            estado_codigo="ACTIVO",
            synced_at=datetime.utcnow() - timedelta(days=days_ago),
        )
        db.add(row); db.commit()

    def test_skips_ruts_answered_recently(self, db, lawyer, court):
        case = _make_case(db, lawyer, court, "C-1-2025")
        _add_litigante(db, case.id, "DDO.", "12345678-9")
        self._cached(db, "12345678-9", days_ago=1)

        client = FakeClient()
        result = sync_sysgal_estados(db, client=client)

        assert client.batches == []
        assert result["consultados"] == 0

    def test_requeries_ruts_whose_answer_went_stale(self, db, lawyer, court):
        case = _make_case(db, lawyer, court, "C-1-2025")
        _add_litigante(db, case.id, "DDO.", "12345678-9")
        self._cached(db, "12345678-9", days_ago=settings.SYSGAL_CACHE_TTL_DAYS + 1)

        client = FakeClient()
        result = sync_sysgal_estados(db, client=client)

        assert result["consultados"] == 1
        assert sorted(r for b in client.batches for r in b) == ["12345678-9"]

    def test_force_ignores_the_freshness_window(self, db, lawyer, court):
        """The admin "refresh now" button must never be a silent no-op."""
        case = _make_case(db, lawyer, court, "C-1-2025")
        _add_litigante(db, case.id, "DDO.", "12345678-9")
        self._cached(db, "12345678-9", days_ago=0)

        client = FakeClient()
        result = sync_sysgal_estados(db, client=client, force=True)

        assert result["consultados"] == 1

    def test_requeries_ruts_never_asked(self, db, lawyer, court):
        case = _make_case(db, lawyer, court, "C-1-2025")
        _add_litigante(db, case.id, "DDO.", "12345678-9")

        client = FakeClient()
        result = sync_sysgal_estados(db, client=client)

        assert result["consultados"] == 1

    def test_no_ruts_makes_no_request(self, db, lawyer, court):
        _make_case(db, lawyer, court, "C-1-2025", abandono_disponible=True)
        client = FakeClient()
        result = sync_sysgal_estados(db, client=client)
        assert client.batches == []
        assert result["consultados"] == 0
        assert result["chunks"] == 0


class TestChunking:
    def test_chunks_at_100(self, db, lawyer, court):
        case = _make_case(db, lawyer, court, "C-1-2025", en_apremio=True)
        for i in range(150):
            _add_litigante(db, case.id, "DDO.", f"{10000000 + i}-1")
        client = FakeClient()
        result = sync_sysgal_estados(db, client=client)
        assert len(client.batches) == 2
        assert max(len(b) for b in client.batches) == 100
        assert result["chunks"] == 2
        assert result["consultados"] == 150


class TestUpsert:
    def test_found_and_not_found_are_stored(self, db, lawyer, court):
        case = _make_case(db, lawyer, court, "C-1-2025", abandono_disponible=True)
        _add_litigante(db, case.id, "DDO.", "12.345.678-9")
        _add_litigante(db, case.id, "DDO.", "23456789-0")

        client = FakeClient(answers={"12345678-9": _found()})
        result = sync_sysgal_estados(db, client=client, today=date(2026, 9, 2))

        assert result["encontrados"] == 1
        assert result["no_encontrados"] == 1
        assert result["errores"] == 0

        found = db.query(ClienteSysgalEstado).filter_by(rut="12345678-9").one()
        assert found.encontrado is True
        assert found.estado_codigo == "ACTIVO"
        assert found.estado_label == "Activo"
        assert found.tiene_contrato is True
        assert found.vigencia_hasta == date(2027, 1, 31)
        assert found.sysgal_updated_at == datetime(2026, 8, 1, 12, 34, 56, 123456)
        assert found.synced_at is not None

        missing = db.query(ClienteSysgalEstado).filter_by(rut="23456789-0").one()
        assert missing.encontrado is False
        assert missing.estado_codigo is None
        assert missing.estado_label is None
        assert missing.tiene_contrato is None
        assert missing.vigencia_hasta is None
        assert missing.sysgal_updated_at is None

    def test_no_pii_columns_on_model(self):
        cols = {c.name for c in ClienteSysgalEstado.__table__.columns}
        assert not ({"nombre", "email", "telefono"} & cols)

    def test_no_contrato_leaves_vigencia_none(self, db, lawyer, court):
        case = _make_case(db, lawyer, court, "C-1-2025", abandono_disponible=True)
        _add_litigante(db, case.id, "DDO.", "12345678-9")
        client = FakeClient(answers={"12345678-9": _found(code="SIN_CONTRATO", hasta=None)})
        sync_sysgal_estados(db, client=client)
        row = db.query(ClienteSysgalEstado).filter_by(rut="12345678-9").one()
        assert row.vigencia_hasta is None
        assert row.tiene_contrato is False

    def test_bad_updated_at_is_none(self, db, lawyer, court):
        case = _make_case(db, lawyer, court, "C-1-2025", abandono_disponible=True)
        _add_litigante(db, case.id, "DDO.", "12345678-9")
        client = FakeClient(answers={"12345678-9": _found(updated="garbage")})
        sync_sysgal_estados(db, client=client)
        row = db.query(ClienteSysgalEstado).filter_by(rut="12345678-9").one()
        assert row.sysgal_updated_at is None
        assert row.encontrado is True

    def test_second_run_updates_not_duplicates(self, db, lawyer, court):
        case = _make_case(db, lawyer, court, "C-1-2025", abandono_disponible=True)
        _add_litigante(db, case.id, "DDO.", "12345678-9")

        sync_sysgal_estados(db, client=FakeClient(answers={"12345678-9": _found(code="ACTIVO")}))
        # force=True: within the TTL the worker would skip this RUT, but an
        # explicit refresh must still reach Sysgal.
        sync_sysgal_estados(
            db,
            client=FakeClient(answers={"12345678-9": _found(code="MOROSO_INACTIVO", label="Moroso")}),
            force=True,
        )

        rows = db.query(ClienteSysgalEstado).filter_by(rut="12345678-9").all()
        assert len(rows) == 1
        assert rows[0].estado_codigo == "MOROSO_INACTIVO"
        assert rows[0].estado_label == "Moroso"


class TestSafeFail:
    def test_failing_chunk_counted_and_does_not_abort(self, db, lawyer, court):
        case = _make_case(db, lawyer, court, "C-1-2025", en_apremio=True)
        for i in range(150):
            _add_litigante(db, case.id, "DDO.", f"{10000000 + i}-1")

        client = FakeClient(fail_batches={0})
        result = sync_sysgal_estados(db, client=client)

        assert len(client.batches) == 2
        assert result["errores"] == 1
        assert result["chunks"] == 2
        # Second chunk (50 ruts) still persisted
        assert db.query(ClienteSysgalEstado).count() == 50
        assert result["no_encontrados"] == 50


# ---------------------------------------------------------------------------
# Wire format: dotted RUT on the request, canonical key in the cache
# ---------------------------------------------------------------------------


class TestWireFormat:
    def test_sends_dotted_when_valid_canonical_when_not_and_keys_cache_canonically(
        self, db, lawyer, court
    ):
        """Sysgal is verified to accept the dotted form; the undotted form is
        not. A RUT that validates is sent dotted; one with a bad verification
        digit (format_rut → None) falls back to canonical. Either way the cache
        row is keyed by the canonical form."""
        from app.utils.rut import calculate_verification_digit, format_rut

        num, num2 = 14183245, 12345678
        valid = f"{num}-{calculate_verification_digit(num)}"
        good_dv = calculate_verification_digit(num2)
        invalid = f"{num2}-{'0' if good_dv != '0' else '1'}"
        assert format_rut(valid) is not None and format_rut(invalid) is None  # sanity

        case = _make_case(db, lawyer, court, "C-9001-2026", en_apremio=True)
        _add_litigante(db, case.id, "DDO.", valid)
        _add_litigante(db, case.id, "DDO.", invalid)

        client = FakeClient(answers={valid: _found(), invalid: _found(code="TERMINADO", label="Terminado")})
        result = sync_sysgal_estados(db, client=client)

        assert result["errores"] == 0 and result["encontrados"] == 2
        sent = client.batches[0]
        assert format_rut(valid) in sent            # dotted on the wire, e.g. "14.183.245-K"
        assert valid not in sent                    # never the undotted form for a valid RUT
        assert invalid in sent                      # invalid dv → canonical fallback

        rows = {r.rut: r for r in db.query(ClienteSysgalEstado).all()}
        assert set(rows) == {valid, invalid}        # cache keyed canonically, never dotted
        assert rows[valid].encontrado and rows[valid].estado_codigo == "ACTIVO"
        assert rows[invalid].encontrado and rows[invalid].estado_codigo == "TERMINADO"
