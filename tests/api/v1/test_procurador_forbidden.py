"""A procurador gets 403 on every endpoint family outside his five screens (S4)."""
import pytest

from tests.api.v1.procurador_support import headers, link, make_lawyer

FORBIDDEN = [
    # admin panels / cuentas / credenciales / auditoría
    ("GET", "/api/v1/lawyers/accounts"),
    ("POST", "/api/v1/lawyers"),
    ("PUT", "/api/v1/lawyers/1/account"),
    ("GET", "/api/v1/credentials/status"),
    ("GET", "/api/v1/activity"),
    ("GET", "/api/v1/stats/admin"),
    ("GET", "/api/v1/stats/firm"),
    ("GET", "/api/v1/stats/risk-board"),
    ("GET", "/api/v1/stats/scraping-status"),
    # traspaso / asignación / liberaciones
    ("GET", "/api/v1/asignacion/desajustes"),
    ("GET", "/api/v1/liberaciones"),
    ("POST", "/api/v1/liberaciones"),
    # bono / liquidación / evaluaciones / renovaciones
    ("GET", "/api/v1/bono/liquidacion"),
    ("GET", "/api/v1/evaluaciones/resultados"),
    ("GET", "/api/v1/renovaciones"),
    # reportes / Sysgal / avance de cartera / escritos / cartera snapshots / matriz
    ("GET", "/api/v1/sysgal/cartera/reporte"),
    ("GET", "/api/v1/avance-cartera/estadisticas"),
    ("GET", "/api/v1/escritos-sysgal/listado"),
    ("GET", "/api/v1/cartera/snapshots"),
    ("GET", "/api/v1/matriz/distribucion"),
    # alerts / webhooks / sync / scrapper / pjud
    ("GET", "/api/v1/alerts"),
    ("POST", "/api/v1/alerts/read-all"),
    ("GET", "/api/v1/webhooks"),
    ("POST", "/api/v1/sync"),
    ("GET", "/api/v1/scrapper/search"),
    # case mutations and other case-family endpoints
    ("DELETE", "/api/v1/cases/1"),
    ("POST", "/api/v1/cases/1/deadlines"),
    ("PUT", "/api/v1/cases/1/deadlines/1/status"),
    ("POST", "/api/v1/cases/1/documents/generate"),
    ("GET", "/api/v1/cases/diagnostico"),
    ("GET", "/api/v1/cases/deadlines/audited"),
    ("GET", "/api/v1/documents/search"),
    # hito approval / admin
    ("POST", "/api/v1/hitos/1/aprobar"),
    ("POST", "/api/v1/hitos/1/rechazar"),
    ("POST", "/api/v1/hitos/aprobar-lote"),
    ("POST", "/api/v1/hitos/eliminar-lote"),
    ("GET", "/api/v1/hitos/resumen"),
    ("GET", "/api/v1/hitos/form-links"),
    ("POST", "/api/v1/hitos/importar"),
    ("PUT", "/api/v1/goals/monthly_productivity"),
]


@pytest.fixture
def proc(db):
    a = make_lawyer(db, "11111111-1", "Abogado A")
    p = make_lawyer(db, "44444444-4", "Proc", role="procurador")
    link(db, p, a)
    return p


@pytest.mark.parametrize("method,path", FORBIDDEN)
def test_forbidden_family_is_403(client, proc, method, path):
    r = client.request(method, path, headers=headers(proc.rut))
    assert r.status_code == 403, f"{method} {path} -> {r.status_code} {r.text[:200]}"
    assert r.json()["detail"] == "Tu perfil de procurador no tiene acceso a esta sección."


@pytest.mark.parametrize("path", [
    "/api/v1/cases", "/api/v1/cases/summary", "/api/v1/calendar", "/api/v1/stats/me",
    "/api/v1/cases/deadlines/semaforo", "/api/v1/hitos", "/api/v1/hitos/tipos",
    "/api/v1/hitos/abogados", "/api/v1/auth/me",
])
def test_allowed_family_is_not_blocked(client, proc, path):
    r = client.get(path, headers=headers(proc.rut))
    assert r.status_code == 200, f"{path} -> {r.status_code} {r.text[:200]}"


def test_public_hito_form_is_untouched_by_the_gate(client, proc):
    assert client.get("/api/v1/hitos/public/nope").status_code == 404


def test_unauthenticated_still_401(client, proc):
    assert client.get("/api/v1/lawyers/accounts").status_code == 401
