"""Tests for the template-based document generator (document_templates.py).

Deterministic and offline: they render the REAL committed .docx template with a
fake case + litigantes and assert the derived variables fill in, unknown facts
become «INDICAR …» prompts, and no raw {{ placeholder }} survives.
"""
import io
import re
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from docx import Document

from app.services.document_templates import (
    TEMPLATE_REGISTRY,
    _derive_sjl,
    is_template_document,
    render_template_document,
)


def _lit(participante, nombre, rut="", persona_type="natural"):
    return SimpleNamespace(participante=participante, nombre=nombre, rut=rut, persona_type=persona_type)


def _render(case, litigantes, court_name, **lawyer):
    data = render_template_document(
        document_type="abandono_3anios",
        case=case,
        litigantes=litigantes,
        court_name=court_name,
        acting_lawyer_name=lawyer.get("name"),
        acting_lawyer_rut=lawyer.get("rut"),
        acting_lawyer_email=lawyer.get("email"),
    )
    text = "\n".join(p.text for p in Document(io.BytesIO(data)).paragraphs)
    return data, text


def test_abandono_renders_with_full_case_data():
    case = SimpleNamespace(
        rol="C-12111-2025", plaintiff="BANCO FALABELLA",
        defendant="ZULEMA GARRIDO MORENO", last_movement_at=datetime(2025, 10, 1),
    )
    lits = [
        _lit("DTE.", "BANCO FALABELLA S.A.", "96509660-4"),
        _lit("DDO.", "ZULEMA VERÓNICA GARRIDO MORENO", "18995444-1"),
    ]
    data, text = _render(
        case, lits, "10º Juzgado Civil de Santiago",
        name="FERNANDA ARROYO DIAZ", rut="18914338-9", email="farroyo@segal.cl",
    )
    assert len(data) > 5000  # a real .docx
    # Header + parties filled from case/litigantes.
    assert "C-12111-2025" in text
    assert "10º Juzgado Civil de Santiago" in text
    assert "ZULEMA VERÓNICA GARRIDO MORENO" in text
    assert "S.J.L. Civil de Santiago (10°)" in text  # derived
    # RUTs formatted with thousands separators.
    assert "18.995.444-1" in text  # ejecutado
    assert "18.914.338-9" in text  # abogado
    assert "farroyo@segal.cl" in text
    # Facts the system can't derive are visible prompts.
    assert "«INDICAR PROFESIÓN DEL EJECUTADO»" in text
    assert "«INDICAR MONTO DEMANDADO»" in text
    assert "aprox. 1 de octubre de 2025" in text  # última-gestión hint
    # Nothing left unrendered.
    assert re.search(r"\{\{.*?\}\}", text) is None


def test_missing_data_falls_back_to_prompts_without_crashing():
    case = SimpleNamespace(rol=None, plaintiff=None, defendant=None, last_movement_at=None)
    data, text = _render(case, [], None)
    assert len(data) > 5000
    assert "«INDICAR ROL»" in text
    assert "«INDICAR EJECUTADO»" in text
    assert "«INDICAR ABOGADO PATROCINANTE»" in text
    assert "«INDICAR FECHA ÚLTIMA GESTIÓN ÚTIL»" in text  # no hint when no movement
    assert re.search(r"\{\{.*?\}\}", text) is None


def test_registry_maps_document_to_its_recommendation():
    assert is_template_document("abandono_3anios") is True
    assert is_template_document("escrito_oposicion") is False
    spec = TEMPLATE_REGISTRY["abandono_3anios"]
    assert spec.recommendation_code == "solicitar_abandono"
    assert spec.template_filename == "abandono_3anios.docx"


@pytest.mark.parametrize(
    "court,expected",
    [
        ("10º Juzgado Civil de Santiago", "S.J.L. Civil de Santiago (10°)"),
        ("1º Juzgado Civil de San Miguel", "S.J.L. Civil de San Miguel (1°)"),
        (None, "«INDICAR TRIBUNAL»"),
    ],
)
def test_derive_sjl(court, expected):
    assert _derive_sjl(court) == expected
