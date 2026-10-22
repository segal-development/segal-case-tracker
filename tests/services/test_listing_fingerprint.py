"""Listing fingerprint experiment: observe the "Mis Causas" row, never steer the scrape.

Pins the four constraints of odd/tasks/huella-del-listado.md:
1. observation only (no scraping behaviour change) -- covered by the sync tests
   still passing; here we only assert what is recorded,
2. ``case_token`` is excluded from the fingerprint,
3. nothing listing_* is written unless the fingerprint changed,
4. a fingerprint failure never breaks the sync.
"""
import hashlib
import json
import logging
import re
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.case import Case
from app.models.lawyer import Lawyer
from app.services import sync_service as sync_module
from app.services.listing_fingerprint import (
    LISTING_FINGERPRINT_FIELDS,
    LISTING_RAW_MAX_LEN,
    compute_listing_fingerprint,
    compute_listing_set_fingerprint,
)
from app.services.sync_service import ScrapedCase, SyncService


def _row(**kw):
    base = dict(
        rol="C-1234-2025",
        tribunal="24º Juzgado Civil de Santiago",
        caratulado="BANCO ITAU/FERNANDEZ",
        fecha_ingreso="01/01/2025",
        estado_cuaderno="Tramitación",
        cuaderno="Principal",
        institucion="Corte de Apelaciones",
    )
    base.update(kw)
    return ScrapedCase(**base)


# --------------------------------------------------------------------------
# Pure fingerprint
# --------------------------------------------------------------------------

class TestComputeListingFingerprint:
    def test_is_deterministic_sha256_hex(self):
        fp = compute_listing_fingerprint(_row())
        assert re.fullmatch(r"[0-9a-f]{64}", fp)
        assert fp == compute_listing_fingerprint(_row())

    @pytest.mark.parametrize(
        "field,value",
        [
            ("estado_cuaderno", "Archivada"),
            ("cuaderno", "Apremio Ejecutivo Obligación de Dar"),
            ("tribunal", "1º Juzgado Civil de Santiago"),
            ("caratulado", "OTRO/OTRO"),
            ("fecha_ingreso", "02/01/2025"),
            ("institucion", "Otra"),
        ],
    )
    def test_each_listing_cell_moves_the_fingerprint(self, field, value):
        assert compute_listing_fingerprint(_row()) != compute_listing_fingerprint(
            _row(**{field: value})
        )

    def test_case_token_is_excluded(self):
        """The token is a rotating JWT: if it entered the hash every row would
        change on every pass and the experiment would measure nothing."""
        a = SimpleNamespace(**vars(_row()), case_token="jwt-AAA")
        b = SimpleNamespace(**vars(_row()), case_token="jwt-BBB")
        assert compute_listing_fingerprint(a) == compute_listing_fingerprint(b)

    def test_none_and_empty_string_are_the_same_cell(self):
        # The converter yields None or "" for a missing cell depending on the caller.
        assert compute_listing_fingerprint(
            _row(institucion=None)
        ) == compute_listing_fingerprint(_row(institucion=""))

    def test_cell_boundaries_are_unambiguous(self):
        assert compute_listing_fingerprint(
            _row(estado_cuaderno="ab", cuaderno="c")
        ) != compute_listing_fingerprint(_row(estado_cuaderno="a", cuaderno="bc"))


# --------------------------------------------------------------------------
# Persistence through the real listing upsert
# --------------------------------------------------------------------------

@pytest.fixture()
def engine():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine):
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _firm_lawyer(db, lawyer):
    """sync_cases creates cases under the firm lawyer (Approach C).

    Depends on ``lawyer`` so the individual lawyer keeps its id.
    """
    db.add(Lawyer(rut="16021492-9", name="Firm", is_active=True))
    db.commit()


@pytest.fixture()
def lawyer(db):
    lw = Lawyer(
        rut="12345678-9", name="Abogada Test", email="t@segal.cl", is_active=True,
        created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
    )
    db.add(lw)
    db.commit()
    return lw


@pytest.fixture()
def sync_logs():
    """Own handler on the sync logger. caplog is polluted by other tests in the
    full suite (25 known failures), so this must not depend on it."""
    records = []

    class _H(logging.Handler):
        def emit(self, record):
            records.append(record)

    lg = logging.getLogger(sync_module.__name__)
    h = _H(level=logging.DEBUG)
    old = (lg.level, lg.disabled, logging.root.manager.disable)
    lg.addHandler(h)
    lg.setLevel(logging.DEBUG)
    lg.disabled = False
    logging.disable(logging.NOTSET)
    try:
        yield records
    finally:
        lg.removeHandler(h)
        lg.setLevel(old[0])
        lg.disabled = old[1]
        logging.disable(old[2])


def _sync(db, lawyer, row, **kw):
    rows = row if isinstance(row, list) else [row]
    return SyncService(db).sync_cases(
        lawyer_id=lawyer.id, scraped_cases=rows, competencia="civil", **kw
    )


def _case(db, procedure=None):
    """The one Case, or the one with ``procedure`` (cuaderno is part of the identity,
    so a ROL listed with several cuadernos has one Case per cuaderno)."""
    db.expire_all()
    if procedure is None:
        return db.query(Case).one()
    return db.query(Case).filter(Case.procedure == procedure).one()


def _changed_lines(records):
    return [r for r in records if "listing fingerprint changed" in r.getMessage()]


@contextmanager
def captured_sql(engine):
    stmts = []

    def _l(conn, cur, statement, params, ctx, many):
        stmts.append(statement)

    event.listen(engine, "before_cursor_execute", _l)
    try:
        yield stmts
    finally:
        event.remove(engine, "before_cursor_execute", _l)


class TestPersistence:
    def test_new_case_gets_baseline_without_a_change_log(self, db, lawyer, sync_logs):
        _sync(db, lawyer, _row())
        c = _case(db)
        assert c.listing_fingerprint == compute_listing_fingerprint(_row())
        assert c.listing_fingerprint_at is not None
        assert c.listing_estado_raw == "Tramitación"
        assert c.listing_cuaderno_raw == "Principal"
        assert _changed_lines(sync_logs) == []

    def test_unchanged_row_writes_nothing_listing(self, db, engine, lawyer, sync_logs):
        _sync(db, lawyer, _row())
        first = _case(db).listing_fingerprint_at
        with captured_sql(engine) as stmts:
            _sync(db, lawyer, _row())
        assert _case(db).listing_fingerprint_at == first
        updates = [s for s in stmts if s.lstrip().upper().startswith("UPDATE CASES")]
        assert updates, "sanity: the pre-existing updated_at touch still happens"
        assert not any("listing_" in s for s in updates)
        assert _changed_lines(sync_logs) == []

    def test_changed_row_rewrites_and_logs_old_and_new(self, db, lawyer, sync_logs):
        _sync(db, lawyer, _row())
        old_fp = _case(db).listing_fingerprint
        _sync(db, lawyer, _row(estado_cuaderno="Archivada"))
        c = _case(db)
        assert c.listing_fingerprint != old_fp
        assert c.listing_estado_raw == "Archivada"
        assert c.listing_cuaderno_raw == "Principal"
        lines = _changed_lines(sync_logs)
        assert len(lines) == 1
        msg = lines[0].getMessage()
        for needle in ("C-1234-2025", "Tramitación", "Archivada", "Principal"):
            assert needle in msg

    def test_row_without_baseline_is_baselined_silently(self, db, lawyer, sync_logs):
        """Cases that existed before the migration have NULL fingerprints: the
        first pass records them, but that is not a "change"."""
        _sync(db, lawyer, _row())
        c = _case(db)
        c.listing_fingerprint = None
        c.listing_fingerprint_at = None
        c.listing_estado_raw = None
        c.listing_cuaderno_raw = None
        db.commit()
        _sync(db, lawyer, _row())
        assert _case(db).listing_fingerprint == compute_listing_fingerprint(_row())
        assert _changed_lines(sync_logs) == []

    def test_oversized_raw_values_are_truncated_not_fatal(self, db, lawyer):
        long = "x" * (LISTING_RAW_MAX_LEN + 500)
        result = _sync(db, lawyer, _row(estado_cuaderno=long))
        assert result.errors == []
        c = _case(db)
        assert len(c.listing_estado_raw) == LISTING_RAW_MAX_LEN
        # the hash covers the FULL value, not the truncated one
        assert c.listing_fingerprint == compute_listing_fingerprint(_row(estado_cuaderno=long))


class TestFailureNeverBreaksSync:
    def test_fingerprint_error_is_logged_and_sync_continues(
        self, db, lawyer, sync_logs, monkeypatch
    ):
        _sync(db, lawyer, _row())

        def boom(_):
            raise RuntimeError("fingerprint exploded")

        monkeypatch.setattr(sync_module, "compute_listing_set_fingerprint", boom)
        result = _sync(db, lawyer, _row(caratulado="OTRO/DEUDOR"))
        assert result.errors == []
        assert result.cases_updated == 1
        # the rest of the upsert still happened
        assert _case(db).plaintiff == "OTRO"
        assert any(
            "fingerprint exploded" in r.getMessage() or r.exc_info for r in sync_logs
            if r.levelno >= logging.WARNING
        )


# --------------------------------------------------------------------------
# Fingerprint of the SET of listing rows of a ROL
# --------------------------------------------------------------------------

def _legacy_single_row_hash(row) -> str:
    """The #339 formula, re-implemented independently of the module under test.

    Constraint 5: with ONE row per ROL the new code must produce exactly this,
    otherwise every already-baselined case would 'change' on the first pass.
    """
    cells = [(getattr(row, n, None) or "") for n in LISTING_FINGERPRINT_FIELDS]
    payload = json.dumps(cells, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


PRINCIPAL = dict(cuaderno="Principal")
INCIDENTE = dict(cuaderno="Incidente Abandono del Proc.")
MEDIDA = dict(cuaderno="Medida prejudicial")


class TestSetFingerprint:
    def test_single_row_equals_legacy_hash(self):
        for kw in ({}, {"estado_cuaderno": "Archivada"}, {"institucion": None}):
            row = _row(**kw)
            assert compute_listing_set_fingerprint([row]) == _legacy_single_row_hash(row)
            assert compute_listing_fingerprint(row) == _legacy_single_row_hash(row)

    def test_row_order_does_not_matter(self):
        a, b, c = _row(**PRINCIPAL), _row(**INCIDENTE), _row(**MEDIDA)
        assert compute_listing_set_fingerprint([a, b, c]) == compute_listing_set_fingerprint(
            [c, a, b]
        )

    def test_duplicate_rows_collapse(self):
        a, b = _row(**PRINCIPAL), _row(**INCIDENTE)
        assert compute_listing_set_fingerprint([a, a, b]) == compute_listing_set_fingerprint(
            [a, b]
        )
        # and one row twice is still the single-row hash (constraint 5)
        assert compute_listing_set_fingerprint([a, a]) == _legacy_single_row_hash(a)

    def test_a_new_row_moves_the_fingerprint(self):
        a, b = _row(**PRINCIPAL), _row(**INCIDENTE)
        assert compute_listing_set_fingerprint([a]) != compute_listing_set_fingerprint([a, b])

    def test_a_changed_cell_in_one_row_moves_the_fingerprint(self):
        a, b = _row(**PRINCIPAL), _row(**INCIDENTE)
        b2 = _row(estado_cuaderno="Archivada", **INCIDENTE)
        assert compute_listing_set_fingerprint([a, b]) != compute_listing_set_fingerprint(
            [a, b2]
        )

    def test_case_token_is_excluded_from_the_set(self):
        a = SimpleNamespace(**vars(_row(**PRINCIPAL)), case_token="jwt-AAA")
        a2 = SimpleNamespace(**vars(_row(**PRINCIPAL)), case_token="jwt-BBB")
        b = SimpleNamespace(**vars(_row(**INCIDENTE)), case_token="jwt-CCC")
        b2 = SimpleNamespace(**vars(_row(**INCIDENTE)), case_token="jwt-DDD")
        assert compute_listing_set_fingerprint([a, b]) == compute_listing_set_fingerprint(
            [b2, a2]
        )

    def test_row_boundaries_are_unambiguous(self):
        # {(x,y)} vs {(x),(y)}-style collisions: two rows must not hash like one.
        a, b = _row(estado_cuaderno="a", cuaderno="b"), _row(estado_cuaderno="c", cuaderno="d")
        merged = _row(estado_cuaderno="a", cuaderno="b")
        assert compute_listing_set_fingerprint([a, b]) != compute_listing_set_fingerprint(
            [merged]
        )


class TestSetThroughTheRealUpsert:
    def test_row_arrival_order_is_irrelevant_across_passes(self, db, lawyer, sync_logs):
        """The bug: 3 rows for one ROL, last-processed wins. Reversing the
        arrival order must now be silent and must not rewrite anything."""
        rows = [_row(**PRINCIPAL), _row(**INCIDENTE), _row(**MEDIDA)]
        _sync(db, lawyer, rows)
        first = _case(db, PRINCIPAL["cuaderno"])
        fp, at = first.listing_fingerprint, first.listing_fingerprint_at
        _sync(db, lawyer, list(reversed(rows)))
        c = _case(db, PRINCIPAL["cuaderno"])
        assert c.listing_fingerprint == fp
        assert c.listing_fingerprint_at == at
        assert _changed_lines(sync_logs) == []

    def test_a_new_cuaderno_is_a_change_with_row_counts(self, db, lawyer, sync_logs):
        _sync(db, lawyer, [_row(**PRINCIPAL)])
        _sync(db, lawyer, [_row(**PRINCIPAL), _row(**INCIDENTE)])
        lines = _changed_lines(sync_logs)
        assert len(lines) == 1
        msg = lines[0].getMessage()
        assert "rows=1->2" in msg
        assert "changed=cuaderno" in msg

    def test_a_new_cuaderno_is_a_second_case_and_the_rol_set_still_changes(
        self, db, lawyer, sync_logs
    ):
        """``cuaderno`` is part of the case identity (rol, court, procedure): the
        same ROL listed with a NEW cuaderno creates a second Case row instead of
        rewriting the first one. The fingerprint observation groups listing rows
        by ROL, so the already-known Case still sees the ROL-level set change.
        """
        _sync(db, lawyer, [_row(**PRINCIPAL)])
        _sync(db, lawyer, [_row(**PRINCIPAL), _row(**INCIDENTE)])
        assert db.query(Case).count() == 2
        assert _case(db, PRINCIPAL["cuaderno"]).listing_fingerprint == _case(
            db, INCIDENTE["cuaderno"]
        ).listing_fingerprint
        (line,) = _changed_lines(sync_logs)
        assert "rows=1->2" in line.getMessage()

    def test_duplicate_rows_in_one_pass_are_one_row(self, db, lawyer, sync_logs):
        _sync(db, lawyer, [_row(), _row()])
        assert _case(db).listing_fingerprint == _legacy_single_row_hash(_row())
        _sync(db, lawyer, [_row()])
        assert _changed_lines(sync_logs) == []

    def test_rows_of_one_rol_group_regardless_of_rol_case_and_spaces(self, db, lawyer):
        _sync(db, lawyer, [_row(rol="c-1234-2025 ", **PRINCIPAL), _row(**INCIDENTE)])
        # one case per cuaderno, and each fingerprint covers BOTH rows of the ROL
        expected = compute_listing_set_fingerprint(
            [_row(rol="c-1234-2025 ", **PRINCIPAL), _row(**INCIDENTE)]
        )
        for procedure in (PRINCIPAL["cuaderno"], INCIDENTE["cuaderno"]):
            assert _case(db, procedure).listing_fingerprint == expected

    def test_distinct_rols_never_mix(self, db, lawyer):
        _sync(db, lawyer, [_row(rol="C-1-2025"), _row(rol="C-2-2025", **INCIDENTE)])
        by_rol = {c.rol: c for c in db.query(Case).all()}
        assert by_rol["C-1-2025"].listing_fingerprint == _legacy_single_row_hash(
            _row(rol="C-1-2025")
        )
        assert by_rol["C-2-2025"].listing_fingerprint == _legacy_single_row_hash(
            _row(rol="C-2-2025", **INCIDENTE)
        )

    def test_multi_row_raw_columns_do_not_depend_on_order(self, db, lawyer):
        rows = [_row(**PRINCIPAL), _row(**INCIDENTE)]
        _sync(db, lawyer, rows)
        procedure = PRINCIPAL["cuaderno"]
        a = (
            _case(db, procedure).listing_estado_raw,
            _case(db, procedure).listing_cuaderno_raw,
        )
        c = _case(db, procedure)
        c.listing_fingerprint = None
        db.commit()
        _sync(db, lawyer, list(reversed(rows)))
        c = _case(db, procedure)
        assert (c.listing_estado_raw, c.listing_cuaderno_raw) == a

    def test_single_row_raw_columns_keep_none_as_none(self, db, lawyer):
        """#339 stored the cell as received; a None cell must stay NULL."""
        row = _row()
        row.estado_cuaderno = None
        _sync(db, lawyer, row)
        c = _case(db)
        assert c.listing_estado_raw is None
        assert c.listing_cuaderno_raw == "Principal"

    def test_single_row_behaves_exactly_like_before(self, db, lawyer, sync_logs):
        """Constraint 5, end to end: stored fingerprint, raw columns and the
        change semantics of a one-row ROL are those of #339."""
        row = _row()
        _sync(db, lawyer, row)
        c = _case(db)
        assert c.listing_fingerprint == _legacy_single_row_hash(row)
        assert c.listing_estado_raw == "Tramitación"
        assert c.listing_cuaderno_raw == "Principal"
        # a case baselined by #339 (no rows column) is NOT reported as changed
        c.listing_rows_raw = None
        db.commit()
        _sync(db, lawyer, row)
        assert _changed_lines(sync_logs) == []
        assert _case(db).listing_fingerprint == _legacy_single_row_hash(row)


class TestPartialPass:
    def test_incomplete_listing_records_nothing_for_a_new_case(self, db, lawyer, sync_logs):
        _sync(db, lawyer, [_row(**PRINCIPAL)], listing_complete=False)
        c = _case(db)
        assert c.listing_fingerprint is None
        assert c.listing_rows_raw is None
        assert _changed_lines(sync_logs) == []
        # skipping is a deliberate no-op, not a swallowed exception
        assert [r for r in sync_logs if r.levelno >= logging.WARNING] == []

    def test_incomplete_listing_leaves_an_existing_fingerprint_untouched(
        self, db, lawyer, sync_logs
    ):
        full = [_row(**PRINCIPAL), _row(**INCIDENTE)]
        _sync(db, lawyer, full)
        before = _case(db, PRINCIPAL["cuaderno"])
        fp, at, raw = before.listing_fingerprint, before.listing_fingerprint_at, before.listing_rows_raw
        # PJUD cut the listing: only ONE of the two rows arrived
        _sync(db, lawyer, [_row(**PRINCIPAL)], listing_complete=False)
        after = _case(db, PRINCIPAL["cuaderno"])
        assert (after.listing_fingerprint, after.listing_fingerprint_at, after.listing_rows_raw) == (
            fp, at, raw,
        )
        # and the next COMPLETE pass sees no change: the partial one fabricated nothing
        _sync(db, lawyer, full)
        assert _changed_lines(sync_logs) == []

    def test_incomplete_listing_still_syncs_the_case_itself(self, db, lawyer):
        """Observation only: the upsert is untouched by the flag."""
        result = _sync(db, lawyer, [_row(**PRINCIPAL)], listing_complete=False)
        assert result.cases_new == 1
        assert _case(db).procedure == "Principal"

    def test_failed_chunk_commit_does_not_leave_a_fingerprint(
        self, db, lawyer, monkeypatch
    ):
        """A pass that dies mid-way: the fingerprint is written in the same
        transaction as the case, so a rolled-back chunk records nothing."""
        monkeypatch.setattr(sync_module.settings, "SYNC_COMMIT_CHUNK_SIZE", 1)
        rows = [_row(rol="C-1-2025"), _row(rol="C-2-2025"), _row(rol="C-3-2025")]
        svc = SyncService(db)
        real = svc._commit_with_retry
        calls = {"n": 0}

        def flaky(work_fn, caches=()):
            calls["n"] += 1
            if calls["n"] == 2:  # second chunk blows up for good
                raise RuntimeError("connection lost")
            return real(work_fn, caches=caches)

        monkeypatch.setattr(svc, "_commit_with_retry", flaky)
        with pytest.raises(RuntimeError):
            svc.sync_cases(lawyer_id=lawyer.id, scraped_cases=rows, competencia="civil")
        db.rollback()
        recorded = {c.rol for c in db.query(Case).filter(Case.listing_fingerprint.isnot(None))}
        assert recorded == {"C-1-2025"}


class TestChangeLogCarriesAllSevenCells:
    def test_every_cell_logs_old_and_new(self, db, lawyer, sync_logs):
        """Tribunal and cuaderno are part of the causa's identity since T3c, so a
        change there creates a new Case row and is not logged as an in-place
        change: they stay equal here and only the other cells mutate."""
        old = dict(
            rol="C-1234-2025", tribunal="T-same", caratulado="CAR-old", fecha_ingreso="01/01/2025",
            estado_cuaderno="EST-old", cuaderno="CUA-same", institucion="INS-old",
        )
        new = dict(
            rol="C-1234-2025", tribunal="T-same", caratulado="CAR-new", fecha_ingreso="02/02/2025",
            estado_cuaderno="EST-new", cuaderno="CUA-same", institucion="INS-new",
        )
        _sync(db, lawyer, ScrapedCase(**old))
        _sync(db, lawyer, ScrapedCase(**new))
        (line,) = _changed_lines(sync_logs)
        msg = line.getMessage()
        for field in ("tribunal", "caratulado", "fecha_ingreso", "estado_cuaderno", "cuaderno", "institucion"):
            o, n = old[field], new[field]
            assert f"{field}={o!r}->{n!r}" in msg, (field, msg)
        assert "rol='C-1234-2025'->'C-1234-2025'" in msg

    def test_the_line_is_one_greppable_line_naming_what_changed(self, db, lawyer, sync_logs):
        _sync(db, lawyer, _row())
        _sync(db, lawyer, _row(institucion="Otra"))
        (line,) = _changed_lines(sync_logs)
        msg = line.getMessage()
        assert "\n" not in msg
        assert msg.startswith("listing fingerprint changed case_id=")
        assert "changed=institucion " in msg + " "
        assert "estado_cuaderno='Tramitación'->'Tramitación'" in msg

    def test_unknown_old_values_are_marked_not_invented(self, db, lawyer, sync_logs):
        """A case baselined by #339 has no stored old rows: say so, don't guess."""
        _sync(db, lawyer, _row(**PRINCIPAL))
        c = _case(db)
        c.listing_rows_raw = None
        db.commit()
        _sync(db, lawyer, [_row(**PRINCIPAL), _row(**INCIDENTE)])
        (line,) = _changed_lines(sync_logs)
        msg = line.getMessage()
        assert "rows=?->2" in msg
        assert "tribunal=?->" in msg
        # unknown is not "changed": only cuaderno is known to differ here
        assert "changed=cuaderno " in msg
        # the two raw columns #339 did store are still used
        assert "estado_cuaderno='Tramitación'->'Tramitación'" in msg

    def test_old_values_survive_a_chain_of_changes(self, db, lawyer, sync_logs):
        """Tribunal and cuaderno are part of the causa's identity since T3c, so a
        change there creates a new Case row and is not logged as an in-place
        change: the chain mutates ``estado_cuaderno`` instead."""
        _sync(db, lawyer, _row(estado_cuaderno="A"))
        _sync(db, lawyer, _row(estado_cuaderno="B"))
        _sync(db, lawyer, _row(estado_cuaderno="C"))
        msgs = [r.getMessage() for r in _changed_lines(sync_logs)]
        assert "estado_cuaderno='A'->'B'" in msgs[0]
        assert "estado_cuaderno='B'->'C'" in msgs[1]


def test_cell_values_render_in_a_stable_order():
    """Cell values must render in SORTED order, not set-iteration order.

    Without an explicit sort the order comes from set iteration, which depends
    on the interpreter's hash seed: two runs of the same pass could print the
    same change differently, and a human comparing two log lines would see a
    change that is not there.

    Six values are used on purpose. With two, set order happens to match sorted
    order and the test passes even with the sort removed — that is exactly how
    the first version of this test failed to catch the mutant.
    """
    from app.services.listing_fingerprint import (
        canonical_listing_rows,
        per_field_values,
    )

    cuadernos = [
        "Principal",
        "Incidente Abandono del Proc.",
        "Medida prejudicial",
        "Cuaderno de Apremio",
        "Incidente Nulidad",
        "Tercería",
    ]

    def _row(cuaderno):
        return SimpleNamespace(
            rol="C-1-2025", tribunal="T", caratulado="A/B",
            fecha_ingreso="01/01/2025", estado_cuaderno="Tramitación",
            cuaderno=cuaderno, institucion="X",
        )

    rows = [_row(c) for c in cuadernos]
    rendered = per_field_values(canonical_listing_rows(rows))["cuaderno"]

    assert rendered.split(" | ") == sorted(cuadernos)
    # And it does not depend on the order the rows arrived in.
    assert (
        per_field_values(canonical_listing_rows(list(reversed(rows))))["cuaderno"]
        == rendered
    )
