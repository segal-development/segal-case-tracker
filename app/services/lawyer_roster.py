"""Firm roster helpers: co-side firm lawyers for the authenticated account."""

import re
from collections import defaultdict
from typing import Optional
from sqlalchemy.orm import Session

from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.lawyer import Lawyer
from app.utils.rut import normalize_rut

DEMANDANTE_ABOGADO = frozenset({"AB.DTE", "AP.DTE"})
DEMANDADO_ABOGADO = frozenset({"AB.DDO", "AP.DDO"})
ALL_ABOGADO = DEMANDANTE_ABOGADO | DEMANDADO_ABOGADO

_TRAILING_PAREN_RE = re.compile(r"\s*\([^)]*\)\s*$")


def _clean_nombre(nombre: str) -> str:
    return _TRAILING_PAREN_RE.sub("", nombre or "").strip()


def _abogado_litigantes_by_case(db: Session, lawyer_id: int) -> dict[int, list]:
    """Load all abogado-coded litigantes for the account's cases, grouped by case_id."""
    rows = (
        db.query(CaseLitigante)
        .join(Case, Case.id == CaseLitigante.case_id)
        .filter(
            Case.lawyer_id == lawyer_id,
            CaseLitigante.participante.in_(list(ALL_ABOGADO)),
        )
        .all()
    )
    by_case: dict[int, list] = defaultdict(list)
    for row in rows:
        by_case[row.case_id].append(row)
    return dict(by_case)


def firm_roster(db: Session, account_rut: str) -> list[dict]:
    """Return firm lawyers (co-side abogados) across the account's cases.

    For each distinct abogado RUT appearing on the SAME side as the account
    in at least one case, returns {rut, nombre (trailing paren stripped), case_count}.
    Sorted by case_count desc.
    """
    account_rut_norm = normalize_rut(account_rut)
    lawyer = db.query(Lawyer).filter(Lawyer.rut == account_rut_norm).first()
    if not lawyer:
        return []

    by_case = _abogado_litigantes_by_case(db, lawyer.id)

    abogado_cases: dict[str, set[int]] = defaultdict(set)
    abogado_info: dict[str, dict] = {}

    for case_id, litigantes in by_case.items():
        account_side: Optional[frozenset] = None
        for lit in litigantes:
            if normalize_rut(lit.rut) == account_rut_norm:
                if lit.participante in DEMANDANTE_ABOGADO:
                    account_side = DEMANDANTE_ABOGADO
                elif lit.participante in DEMANDADO_ABOGADO:
                    account_side = DEMANDADO_ABOGADO
                break
        if account_side is None:
            continue
        # Include the account holder too — they are a firm lawyer with their own
        # caseload. Every firm-side abogado (account included) is a roster member.
        for lit in litigantes:
            norm = normalize_rut(lit.rut)
            if lit.participante in account_side:
                abogado_cases[norm].add(case_id)
                if norm not in abogado_info:
                    abogado_info[norm] = {"rut": norm, "nombre": _clean_nombre(lit.nombre)}

    result = [
        {"rut": abogado_info[n]["rut"], "nombre": abogado_info[n]["nombre"], "case_count": len(ids)}
        for n, ids in abogado_cases.items()
    ]
    result.sort(key=lambda x: x["case_count"], reverse=True)
    return result


def case_ids_for_abogado(db: Session, account_rut: str, abogado_rut: str) -> set[int]:
    """Return case IDs where abogado_rut is a firm-side abogado (same side as account)."""
    account_rut_norm = normalize_rut(account_rut)
    abogado_rut_norm = normalize_rut(abogado_rut)

    lawyer = db.query(Lawyer).filter(Lawyer.rut == account_rut_norm).first()
    if not lawyer:
        return set()

    by_case = _abogado_litigantes_by_case(db, lawyer.id)
    result: set[int] = set()

    for case_id, litigantes in by_case.items():
        account_side: Optional[frozenset] = None
        for lit in litigantes:
            if normalize_rut(lit.rut) == account_rut_norm:
                if lit.participante in DEMANDANTE_ABOGADO:
                    account_side = DEMANDANTE_ABOGADO
                elif lit.participante in DEMANDADO_ABOGADO:
                    account_side = DEMANDADO_ABOGADO
                break
        if account_side is None:
            continue
        for lit in litigantes:
            if normalize_rut(lit.rut) == abogado_rut_norm and lit.participante in account_side:
                result.add(case_id)
                break

    return result
