"""Tests for the escrito-de-oposición generator (system requirement #3, slice 1).

Covers the pure service (party resolution + non-empty docx bytes) and the
streaming endpoint (auth/scope + docx attachment response).
"""

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.security import create_access_token
from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.services.document_generation import (
    build_escrito_oposicion,
    resolve_parties,
)

DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
AUDITOR_RUT = "77777777-7"


def _lit(participante, rut, nombre, persona_type="NATURAL"):
    return SimpleNamespace(
        participante=participante,
        rut=rut,
        nombre=nombre,
        persona_type=persona_type,
    )


class TestResolveParties:
    def test_picks_dte_as_ejecutante_and_ddo_as_ejecutado(self):
        litigantes = [
            _lit("DDO.", "12345678-9", "Juan Pérez (DEUDOR)"),
            _lit("DTE.", "60000000-0", "Banco Ejemplo S.A."),
            _lit("AB.DTE", "11111111-1", "Abogado Contrario"),
        ]
        ejecutante, ejecutado = resolve_parties(litigantes)
        assert ejecutante is not None and ejecutado is not None
        assert ejecutante.nombre == "Banco Ejemplo S.A."
        assert ejecutado.nombre == "Juan Pérez"  # trailing parenthetical stripped
        assert ejecutado.rut == "12345678-9"

    def test_missing_sides_return_none(self):
        ejecutante, ejecutado = resolve_parties([])
        assert ejecutante is None and ejecutado is None


class TestBuildEscrito:
    def _case(self, **overrides):
        defaults = dict(
            rol="C-1234-2025",
            titulo_tipo=None,
            titulo_fecha=None,
            prescripcion_cumplida=False,
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_returns_non_empty_docx_bytes(self):
        data = build_escrito_oposicion(
            case=self._case(),
            litigantes=[
                _lit("DTE.", "60000000-0", "Banco Ejemplo S.A."),
                _lit("DDO.", "12345678-9", "Juan Pérez"),
            ],
            court_name="Santiago",
            acting_lawyer_name="María González",
            acting_lawyer_rut="9999999-9",
        )
        assert isinstance(data, bytes)
        assert len(data) > 0
        assert data[:2] == b"PK"  # .docx is a zip container

    def test_prescripcion_pagare_uses_one_year_basis(self):
        from docx import Document
        import io

        data = build_escrito_oposicion(
            case=self._case(
                titulo_tipo="pagare",
                titulo_fecha=date(2020, 3, 5),
                prescripcion_cumplida=True,
            ),
            litigantes=[],
            court_name=None,
            acting_lawyer_name=None,
            acting_lawyer_rut=None,
        )
        text = "\n".join(p.text for p in Document(io.BytesIO(data)).paragraphs)
        assert "artículo 98 del DFL N°707" in text
        assert "5 de marzo de 2020" in text
        assert "[EJECUTADO]" in text  # placeholder when litigante absent

    def test_falls_back_to_case_plaintiff_defendant_without_litigantes(self):
        from docx import Document
        import io

        # ~83% of cases have no DTE./DDO. litigantes but ~98% carry the
        # free-text plaintiff/defendant — the caratulado must use those.
        data = build_escrito_oposicion(
            case=self._case(plaintiff="Promotora CMR Falabella S.A.", defendant="Sáez"),
            litigantes=[],
            court_name=None,
            acting_lawyer_name=None,
            acting_lawyer_rut=None,
        )
        text = "\n".join(p.text for p in Document(io.BytesIO(data)).paragraphs)
        assert '"Promotora CMR Falabella S.A. con Sáez"' in text
        assert "[EJECUTANTE]" not in text
        assert "[EJECUTADO]" not in text
        assert "RUT [___]" in text  # RUT still a placeholder (not in the free-text fields)


@pytest.fixture
def auditor(db):
    obj = Lawyer(rut=AUDITOR_RUT, name="Auditor", role="auditor")
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


@pytest.fixture
def court(db):
    obj = Court(code="T1-DOCGEN", name="Juzgado Docgen", region="RM", type="civil")
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


@pytest.fixture
def case(db, auditor, court):
    obj = Case(
        lawyer_id=auditor.id,
        court_id=court.id,
        rol="C-4502-2025",
        status="active",
        competencia="civil",
        plaintiff="Banco Ejemplo S.A.",
        defendant="Juan Pérez",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(obj); db.commit(); db.refresh(obj)
    for i, (part, rut, nombre) in enumerate([
        ("DTE.", "60000000-0", "Banco Ejemplo S.A."),
        ("DDO.", "12345678-9", "Juan Pérez"),
    ]):
        db.add(CaseLitigante(
            case_id=obj.id, participante=part, rut=rut,
            persona_type="NATURAL", nombre=nombre, natural_key=f"{obj.id}-{i}",
        ))
    db.commit()
    return obj


def _headers(rut: str) -> dict:
    tok = create_access_token({"sub": rut}, expires_delta=timedelta(minutes=30))
    return {"Authorization": f"Bearer {tok}"}


class TestGenerateEndpoint:
    def test_generates_docx_attachment(self, client, case, auditor):
        resp = client.post(
            f"/api/v1/cases/{case.id}/documents/generate",
            json={"document_type": "escrito_oposicion"},
            headers=_headers(AUDITOR_RUT),
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == DOCX_MEDIA_TYPE
        assert "attachment" in resp.headers["content-disposition"]
        assert "oposicion_excepciones_C_4502_2025.docx" in resp.headers["content-disposition"]
        assert resp.content[:2] == b"PK"

    def test_unknown_document_type_rejected(self, client, case, auditor):
        resp = client.post(
            f"/api/v1/cases/{case.id}/documents/generate",
            json={"document_type": "nope"},
            headers=_headers(AUDITOR_RUT),
        )
        assert resp.status_code == 422

    def test_missing_case_returns_404(self, client, auditor):
        resp = client.post(
            "/api/v1/cases/999999/documents/generate",
            json={"document_type": "escrito_oposicion"},
            headers=_headers(AUDITOR_RUT),
        )
        assert resp.status_code == 404
