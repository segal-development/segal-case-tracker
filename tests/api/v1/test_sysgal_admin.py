"""Tests for the Sysgal admin endpoints: POST /sysgal/sync and GET /sysgal/status."""

from datetime import date, datetime, timedelta

import pytest

from app.core.security import create_access_token
from app.models.cliente_sysgal_estado import ClienteSysgalEstado
from app.models.lawyer import Lawyer

ADMIN_RUT = "11111111-1"
AUDITOR_RUT = "33333333-3"
LAWYER_RUT = "22222222-2"


def _headers(rut):
    token = create_access_token({"sub": rut}, expires_delta=timedelta(minutes=30))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_headers(db):
    db.add(Lawyer(rut=ADMIN_RUT, name="Admin", role="admin")); db.commit()
    return _headers(ADMIN_RUT)


@pytest.fixture
def auditor_headers(db):
    db.add(Lawyer(rut=AUDITOR_RUT, name="Auditor", role="auditor")); db.commit()
    return _headers(AUDITOR_RUT)


@pytest.fixture
def lawyer_headers(db):
    db.add(Lawyer(rut=LAWYER_RUT, name="Lawyer", role="lawyer")); db.commit()
    return _headers(LAWYER_RUT)


class TestSync:
    def test_lawyer_forbidden(self, client, lawyer_headers):
        assert client.post("/api/v1/sysgal/sync", headers=lawyer_headers).status_code == 403

    def test_auditor_forbidden(self, client, auditor_headers):
        assert client.post("/api/v1/sysgal/sync", headers=auditor_headers).status_code == 403

    def test_admin_runs_sync(self, client, admin_headers, monkeypatch):
        import app.api.v1.sysgal as mod

        called = {}

        def fake_sync(db):
            called["db"] = db
            return {
                "skipped": False,
                "consultados": 3,
                "encontrados": 2,
                "no_encontrados": 1,
                "errores": 0,
                "chunks": 1,
            }

        monkeypatch.setattr(mod, "sync_sysgal_estados", fake_sync)
        resp = client.post("/api/v1/sysgal/sync", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["consultados"] == 3
        assert "db" in called

    def test_admin_unconfigured_returns_skipped(self, client, admin_headers, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "SYSGAL_BASE_URL", "")
        monkeypatch.setattr(settings, "SYSGAL_API_KEY", "")
        resp = client.post("/api/v1/sysgal/sync", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["skipped"] is True


class TestStatus:
    def test_lawyer_forbidden(self, client, lawyer_headers):
        assert client.get("/api/v1/sysgal/status", headers=lawyer_headers).status_code == 403

    def test_empty_status(self, client, auditor_headers, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "SYSGAL_BASE_URL", "")
        monkeypatch.setattr(settings, "SYSGAL_API_KEY", "")
        resp = client.get("/api/v1/sysgal/status", headers=auditor_headers)
        assert resp.status_code == 200
        assert resp.json() == {
            "configured": False,
            "last_synced_at": None,
            "total_ruts": 0,
            "por_cobertura": {"activo": 0, "moroso": 0, "caducado": 0, "sin_dato": 0},
        }

    def test_status_counts(self, client, db, admin_headers, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "SYSGAL_BASE_URL", "https://sysgal.example.test")
        monkeypatch.setattr(settings, "SYSGAL_API_KEY", "k")

        rows = [
            ("1-9", True, "ACTIVO", date(2099, 1, 1), datetime(2026, 9, 1, 1, 0)),
            ("2-7", True, "POR_VENCER", date(2099, 1, 1), datetime(2026, 9, 1, 1, 0)),
            ("3-5", True, "MOROSO_INACTIVO", None, datetime(2026, 9, 1, 1, 0)),
            ("4-3", True, "ACTIVO", date(2020, 1, 1), datetime(2026, 9, 1, 2, 0)),
            ("5-1", True, "TERMINADO", None, datetime(2026, 9, 1, 1, 0)),
            ("6-K", False, None, None, datetime(2026, 9, 1, 1, 0)),
        ]
        for rut, enc, code, hasta, synced in rows:
            db.add(
                ClienteSysgalEstado(
                    rut=rut, encontrado=enc, estado_codigo=code, vigencia_hasta=hasta, synced_at=synced
                )
            )
        db.commit()

        resp = client.get("/api/v1/sysgal/status", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["configured"] is True
        assert data["total_ruts"] == 6
        assert data["last_synced_at"].startswith("2026-09-01T02:00:00")
        assert data["por_cobertura"] == {"activo": 2, "moroso": 1, "caducado": 2, "sin_dato": 1}
