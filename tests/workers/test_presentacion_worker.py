"""Tests for the presentación worker (Slice 2a — scaffolding).

No network, no real sleep, no Playwright/PJUD: ``_download_documents`` is mocked
and ``presentar_fn`` is injected. Covers the master gate (disabled → claims
nothing), atomic claiming, and every ``process_one`` state transition including
the Slice-2b stub raising ``NotImplementedError``.
"""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.models.presentacion import (
    PRES_CARGADA_PENDIENTE,
    PRES_EN_COLA,
    PRES_ERROR,
    PRES_PRESENTANDO,
    Presentacion,
)
from app.workers import presentacion_worker as worker
from app.workers.presentacion_worker import (
    TransientPresentacionError,
    claim_next,
    process_one,
    run_once,
)


def _make_presentacion(**overrides) -> Presentacion:
    """Build a minimal valid queued Presentacion row."""
    defaults = dict(
        idempotency_key=f"idem-{datetime.utcnow().timestamp()}-{id(overrides)}",
        tipo_gestion="escrito",
        modo="semiauto",
        credential_ref="12345678-9",
        payload={
            "litigantes": [],
            "documento_principal": {"url": "http://x/p.pdf", "referencia": "P"},
            "documentos": [],
        },
        estado=PRES_EN_COLA,
        intentos=0,
    )
    defaults.update(overrides)
    return Presentacion(**defaults)


class TestRunOnceGate:
    """Master gate OFF → the worker claims/processes nothing."""

    @pytest.mark.asyncio
    async def test_disabled_does_not_claim(self, db):
        row = _make_presentacion()
        db.add(row)
        db.commit()

        with patch.object(worker.settings, "PRESENTACION_WORKER_ENABLED", False):
            await run_once(db)

        db.refresh(row)
        assert row.estado == PRES_EN_COLA
        assert row.intentos == 0


class TestClaimNext:
    """claim_next flips the OLDEST en_cola row to presentando."""

    def test_claims_oldest_and_increments_intentos(self, db):
        now = datetime.utcnow()
        older = _make_presentacion(created_at=now - timedelta(minutes=5))
        newer = _make_presentacion(created_at=now)
        db.add_all([newer, older])
        db.commit()

        claimed = claim_next(db)

        assert claimed is not None
        assert claimed.id == older.id
        assert claimed.estado == PRES_PRESENTANDO
        assert claimed.intentos == 1

        db.refresh(newer)
        assert newer.estado == PRES_EN_COLA

    def test_returns_none_on_empty_queue(self, db):
        assert claim_next(db) is None


class TestProcessOne:
    """Every process_one outcome path."""

    @pytest.mark.asyncio
    async def test_success_sets_returned_estado(self, db):
        row = _make_presentacion(modo="semiauto", estado=PRES_PRESENTANDO, intentos=1)
        db.add(row)
        db.commit()

        presentar_fn = AsyncMock(return_value={"estado": PRES_CARGADA_PENDIENTE})

        with patch.object(
            worker, "_download_documents", new=AsyncMock(return_value={})
        ):
            await process_one(db, row, presentar_fn=presentar_fn)

        db.refresh(row)
        assert row.estado == PRES_CARGADA_PENDIENTE
        presentar_fn.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_not_implemented_goes_to_error(self, db):
        row = _make_presentacion(estado=PRES_PRESENTANDO, intentos=1)
        db.add(row)
        db.commit()

        async def _stub(_p, _f):
            raise NotImplementedError("Slice 2b: no implementado")

        with patch.object(
            worker, "_download_documents", new=AsyncMock(return_value={})
        ):
            # Must NOT raise — process_one swallows everything.
            await process_one(db, row, presentar_fn=_stub)

        db.refresh(row)
        assert row.estado == PRES_ERROR
        assert row.error_detail
        assert "Slice 2b" in row.error_detail

    @pytest.mark.asyncio
    async def test_transient_below_max_requeues(self, db):
        row = _make_presentacion(estado=PRES_PRESENTANDO, intentos=1)
        db.add(row)
        db.commit()

        download = AsyncMock(side_effect=TransientPresentacionError("blip"))

        with patch.object(worker, "_download_documents", new=download), patch.object(
            worker.settings, "PRESENTACION_MAX_INTENTOS", 3
        ):
            await process_one(db, row)

        db.refresh(row)
        assert row.estado == PRES_EN_COLA

    @pytest.mark.asyncio
    async def test_transient_at_max_goes_to_error(self, db):
        row = _make_presentacion(estado=PRES_PRESENTANDO, intentos=3)
        db.add(row)
        db.commit()

        download = AsyncMock(side_effect=TransientPresentacionError("blip"))

        with patch.object(worker, "_download_documents", new=download), patch.object(
            worker.settings, "PRESENTACION_MAX_INTENTOS", 3
        ):
            await process_one(db, row)

        db.refresh(row)
        assert row.estado == PRES_ERROR
        assert row.error_detail
