"""Short Spanish descriptions for activity events, computed from (kind, method, route)."""

from typing import Optional

_SCREENS = {
    "/causas": "Causas",
    "/plazos": "Plazos",
    "/hitos": "Hitos",
    "/agenda": "Agenda",
    "/productividad": "Productividad",
    "/liberaciones": "Liberaciones",
    "/bono": "Bono",
    "/renovaciones": "Renovaciones",
    "/evaluaciones": "Evaluaciones",
    "/matriz": "Matriz",
    "/asignacion": "Asignación",
    "/cartera": "Cartera",
    "/alertas": "Alertas",
    "/actividad": "Actividad",
}

_P = "/api/v1"

_ACTIONS = {
    ("POST", f"{_P}/hitos"): "Creó un hito",
    ("PUT", f"{_P}/hitos/{{hito_id}}"): "Editó un hito",
    ("DELETE", f"{_P}/hitos/{{hito_id}}"): "Eliminó un hito",
    ("POST", f"{_P}/hitos/{{hito_id}}/aprobar"): "Aprobó un hito",
    ("POST", f"{_P}/hitos/{{hito_id}}/rechazar"): "Rechazó un hito",
    ("POST", f"{_P}/hitos/aprobar-lote"): "Aprobó hitos en lote",
    ("POST", f"{_P}/hitos/rechazar-lote"): "Rechazó hitos en lote",
    ("POST", f"{_P}/hitos/eliminar-lote"): "Eliminó hitos en lote",
    ("PUT", f"{_P}/hitos/{{hito_id}}/evidencia"): "Subió evidencia de un hito",
    ("POST", f"{_P}/hitos/importar"): "Importó hitos",
    ("POST", f"{_P}/liberaciones"): "Solicitó una liberación",
    ("POST", f"{_P}/liberaciones/{{req_id}}/aprobar"): "Aprobó una liberación",
    ("POST", f"{_P}/liberaciones/{{req_id}}/rechazar"): "Rechazó una liberación",
    ("PUT", f"{_P}/cases/{{case_id}}/deadlines/{{deadline_id}}/status"): "Cambió el estado de un plazo",
    ("POST", f"{_P}/cases/{{case_id}}/deadlines"): "Creó un plazo",
    ("DELETE", f"{_P}/cases/{{case_id}}"): "Eliminó una causa",
    ("POST", f"{_P}/cases/{{case_id}}/documents/generate"): "Generó un documento",
    ("POST", f"{_P}/auth/change-password"): "Cambió su contraseña",
    ("PUT", f"{_P}/auth/password"): "Estableció la contraseña de una cuenta",
    ("POST", f"{_P}/auth/logout"): "Cerró sesión",
    ("POST", f"{_P}/asignacion/reasignar"): "Reasignó una causa",
    ("POST", f"{_P}/asignacion/automatica"): "Ejecutó la asignación automática",
    ("POST", f"{_P}/lawyers"): "Creó una cuenta",
    ("PUT", f"{_P}/lawyers/{{id}}/account"): "Editó una cuenta",
    ("DELETE", f"{_P}/lawyers/{{id}}"): "Eliminó una cuenta",
    ("POST", f"{_P}/sync"): "Sincronizó causas",
    ("POST", f"{_P}/scrapper/refresh/{{case_id}}"): "Actualizó una causa desde PJUD",
    ("POST", f"{_P}/bono/cierre"): "Cerró el bono del mes",
    ("POST", f"{_P}/bono/reabrir"): "Reabrió el bono del mes",
}


def label_for(kind: str, method: Optional[str], route: str) -> str:
    if kind == "login":
        return "Inició sesión"
    if kind == "login_failed":
        return "Intento de inicio de sesión fallido"
    if kind == "view":
        if route.startswith("/causas/"):
            return "Vio la causa"
        screen = _SCREENS.get(route.rstrip("/") or "/")
        return f"Vio {screen}" if screen else f"Vio {route}"
    if "export" in route.lower().split("/")[-1]:
        return "Exportó"
    known = _ACTIONS.get(((method or "").upper(), route))
    if known:
        return known
    return f"{method} {route}" if method else route
