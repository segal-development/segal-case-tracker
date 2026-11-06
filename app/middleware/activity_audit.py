"""ASGI middleware that records mutating /api/v1 calls and logins (best effort).

Pure ASGI on purpose: it wraps ``send`` to see the status code and ``receive``
to capture the body ONLY on the three login routes, where ONLY the identifier
field (email / RUT) is ever read. No other body, query string or header (except
User-Agent) is read or stored. Everything after the response is wrapped so the
audit can never break or fail a request.
"""

import json
import logging
from typing import Optional
from urllib.parse import parse_qs

from starlette.concurrency import run_in_threadpool

from app.services import activity_audit as audit

logger = logging.getLogger(__name__)

_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
_EXCLUDED_PREFIXES = ("/api/v1/pjud/ingest", "/api/v1/activity", "/api/v1/health")
# route -> field holding the submitted identifier
_LOGIN_ROUTES = {
    "/api/v1/auth/web-login": "email",
    "/api/v1/auth/login": "rut",
    "/api/v1/auth/login/clave-unica": "rut",
}
_MAX_LOGIN_BODY = 16 * 1024


def _is_excluded(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in _EXCLUDED_PREFIXES)


def _identifier_from_body(body: bytes, content_type: str, field: str) -> Optional[str]:
    try:
        if "json" in content_type:
            value = json.loads(body.decode("utf-8")).get(field)
        else:
            vals = parse_qs(body.decode("utf-8")).get(field)
            value = vals[0] if vals else None
    except Exception:
        return None
    return value.strip()[:120] if isinstance(value, str) and value.strip() else None


# Path params whose value is a credential (the public hito form links carry a bearer-style
# {token}): never stored, neither in `path` nor in `target`.
_SECRET_PARAM_HINTS = ("token", "secret", "key")


def _is_secret_param(name: str) -> bool:
    return any(h in name.lower() for h in _SECRET_PARAM_HINTS)


def _redact_path(path: str, path_params: dict) -> str:
    """Replace each secret path-param value by *** (whole path segments only)."""
    secrets = {str(v) for k, v in path_params.items() if _is_secret_param(k) and str(v)}
    if not secrets:
        return path
    return "/".join("***" if seg in secrets else seg for seg in path.split("/"))


def _case_and_target(path_params: dict, path: str):
    target = None
    for k, v in path_params.items():
        if _is_secret_param(k):
            continue
        target = str(v)
        break
    case_raw = path_params.get("case_id")
    if case_raw is None and path.startswith("/api/v1/cases/"):
        case_raw = path_params.get("id")
    try:
        case_id = int(case_raw) if case_raw is not None else None
    except (TypeError, ValueError):
        case_id = None
    return case_id, target


class ActivityAuditMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in _MUTATING:
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if not path.startswith("/api/v1") or _is_excluded(path) or not audit.ENABLED:
            return await self.app(scope, receive, send)

        status = {"code": None}
        body = bytearray()
        capture = path in _LOGIN_ROUTES

        async def receive_wrapper():
            message = await receive()
            if capture and message["type"] == "http.request" and len(body) < _MAX_LOGIN_BODY:
                body.extend(message.get("body", b"")[: _MAX_LOGIN_BODY - len(body)])
            return message

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive_wrapper if capture else receive, send_wrapper)
        finally:
            await self._record(scope, path, status["code"], bytes(body))

    async def _record(self, scope, path, status_code, body: bytes) -> None:
        try:
            headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                       for k, v in scope.get("headers", [])
                       if k in (b"authorization", b"user-agent", b"content-type")}
            client = scope.get("client")
            route_obj = scope.get("route")
            route = getattr(route_obj, "path", None) or path
            params = scope.get("path_params") or {}
            case_id, target = _case_and_target(params, path)
            stored_path = _redact_path(path, params)
            await run_in_threadpool(
                self._write, stored_path, route, scope["method"], status_code, headers,
                client[0] if client else None, case_id, target, body,
            )
        except Exception as exc:  # S3: the audit never breaks a request
            logger.warning("activity audit failed: %s", exc)

    @staticmethod
    def _write(path, route, method, status_code, headers, ip, case_id, target, body):
        db = audit.new_session()
        try:
            common = {
                "method": method, "route": route, "path": path, "status_code": status_code,
                "ip": ip, "user_agent": headers.get("user-agent"),
            }
            field = _LOGIN_ROUTES.get(path)
            if field is not None:
                if status_code is not None and 200 <= status_code < 300:
                    ident = _identifier_from_body(body, headers.get("content-type", ""), field)
                    lawyer = _find_login_lawyer(db, field, ident)
                    audit.insert_event(db, kind="login", **audit.actor_fields(lawyer), **common)
                    return
                if status_code in (401, 403):
                    ident = _identifier_from_body(body, headers.get("content-type", ""), field)
                    audit.insert_event(db, kind="login_failed", identifier=ident,
                                       **audit.actor_fields(None), **common)
                    return
            lawyer = audit.lawyer_from_token(db, headers.get("authorization"))
            audit.insert_event(db, kind="action", case_id=case_id, target=target,
                               **audit.actor_fields(lawyer), **common)
        finally:
            db.close()


def _find_login_lawyer(db, field: str, ident: Optional[str]):
    if not ident:
        return None
    from sqlalchemy import func

    from app.models.lawyer import Lawyer

    if field == "email":
        return db.query(Lawyer).filter(func.lower(Lawyer.email) == ident.lower()).first()
    return db.query(Lawyer).filter(Lawyer.rut == ident).first()
