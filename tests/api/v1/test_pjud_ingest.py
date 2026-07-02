"""Tests for POST /api/v1/pjud/ingest/cases.

Auth is via the X-Ingest-Key header (require_ingest_key), NOT the lawyer
JWT — this endpoint is called by the browser extension's service worker,
not by a logged-in lawyer.
"""

import hashlib

import pytest

from app.models.ingest_key import IngestKey

INGEST_URL = "/api/v1/pjud/ingest/cases"


def _case_row(token: str, rol: str, tribunal: str, caratulado: str) -> str:
    return f"""
    <tr>
      <td><a onclick="detalleMisCausaCivil('{token}')">Ver</a></td>
      <td>{rol}</td>
      <td>{tribunal}</td>
      <td>{caratulado}</td>
      <td>01/01/2026</td>
      <td>En tramitacion</td>
      <td>Principal</td>
      <td>-</td>
    </tr>
    """


def _mis_causas_page(rows: list[str]) -> str:
    return f"""
    <html><body>
      Total de registros: {len(rows)}
      <table>{''.join(rows)}</table>
    </body></html>
    """


VALID_PAGE = _mis_causas_page(
    [_case_row("TOKEN1", "C-1234-2026", "1 Juzgado Civil de Santiago", "BANCO DE CHILE / PEREZ")]
)
MALFORMED_PAGE = "<html><body>Not a PJUD page.</body></html>"


@pytest.fixture
def ingest_key(db) -> str:
    """Create an active IngestKey and return its plaintext value."""
    plaintext = "test-ingest-key-plaintext"
    key_hash = hashlib.sha256(plaintext.encode()).hexdigest()
    db.add(IngestKey(label="operator-test", key_hash=key_hash, is_active=True))
    db.commit()
    return plaintext


class TestIngestCasesEndpointAuth:
    def test_missing_key_returns_401(self, client):
        response = client.post(
            INGEST_URL,
            json={"rut": "11111111-1", "competencia": "civil", "pages": [VALID_PAGE]},
        )
        assert response.status_code == 401

    def test_invalid_key_returns_403(self, client, ingest_key):
        response = client.post(
            INGEST_URL,
            json={"rut": "11111111-1", "competencia": "civil", "pages": [VALID_PAGE]},
            headers={"X-Ingest-Key": "wrong-key"},
        )
        assert response.status_code == 403


class TestIngestCasesEndpointHappyPath:
    def test_valid_payload_returns_counts(self, client, ingest_key):
        response = client.post(
            INGEST_URL,
            json={"rut": "11111111-1", "competencia": "civil", "pages": [VALID_PAGE]},
            headers={"X-Ingest-Key": ingest_key},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["new"] == 1
        assert data["existing"] == 0
        assert data["errors"] == []

    def test_malformed_html_returns_4xx_and_no_500(self, client, ingest_key):
        response = client.post(
            INGEST_URL,
            json={"rut": "11111111-1", "competencia": "civil", "pages": [MALFORMED_PAGE]},
            headers={"X-Ingest-Key": ingest_key},
        )
        assert 400 <= response.status_code < 500

    def test_repeat_ingest_returns_zero_new(self, client, ingest_key):
        client.post(
            INGEST_URL,
            json={"rut": "11111111-1", "competencia": "civil", "pages": [VALID_PAGE]},
            headers={"X-Ingest-Key": ingest_key},
        )
        response = client.post(
            INGEST_URL,
            json={"rut": "11111111-1", "competencia": "civil", "pages": [VALID_PAGE]},
            headers={"X-Ingest-Key": ingest_key},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["new"] == 0
        assert data["existing"] == 1
