"""Shared builders for the procurador tests (accounts, cases, tokens)."""
from datetime import datetime, timedelta

from app.core.security import create_access_token
from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.procurador import ProcuradorAbogado

MSG_LINKS = "Un procurador debe apoyar a 1 o 2 abogados."
MSG_NOT_SUPPORTED = "Solo puedes ingresar hitos de los abogados que apoyas."


def headers(rut):
    tok = create_access_token({"sub": rut}, expires_delta=timedelta(minutes=30))
    return {"Authorization": f"Bearer {tok}"}


def make_lawyer(db, rut, name, role="lawyer", **kw):
    kw.setdefault("is_firm_lawyer", role == "lawyer")
    obj = Lawyer(rut=rut, name=name, role=role, **kw)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def link(db, procurador, *abogados):
    for ab in abogados:
        db.add(ProcuradorAbogado(procurador_id=procurador.id, abogado_id=ab.id))
    db.commit()


def make_court(db):
    c = Court(code="T1-PRO", name="Juzgado Proc", region="RM", type="civil")
    db.add(c)
    db.commit()
    return c


def make_case(db, owner, court, rol, abogado):
    """Case whose abogado-of-record litigante is ``abogado`` (litigante-derived scope)."""
    case = Case(
        lawyer_id=owner.id, court_id=court.id, rol=rol, status="active", competencia="civil",
        plaintiff="Banco X", defendant="Deudor Y",
        created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
    )
    db.add(case)
    db.commit()
    db.refresh(case)
    db.add(CaseLitigante(
        case_id=case.id, participante="AB.DDO", rut=abogado.rut, persona_type="NATURAL",
        nombre=abogado.name, natural_key=f"{case.id}-{abogado.rut}",
    ))
    db.commit()
    return case
