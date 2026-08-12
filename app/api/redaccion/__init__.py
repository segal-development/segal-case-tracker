"""External read-only API for the Redaccion (document-drafters) system.

Additive and ISOLATED from the internal app AND from the Sysgal API: its own
auth (``X-API-Key`` / ``RedaccionApiKey``), its own routers, mounted separately
at ``/api/redaccion/v1`` in ``app.main`` — NOT under ``/api/v1`` and NOT under
``/api/sysgal/v1``. Never mutates data (only stamps ``last_used_at`` on the key).

Purpose: an external document-drafting system searches a causa (by ROL/RIT or
by client RUT) and reads the FULL case detail needed to draft filings —
identificación, estado, etapa del procedimiento, acción recomendada, plazos,
partes, notificaciones/escritos/exhortos, and the movement history.
Documents/PDFs/files are explicitly OUT OF SCOPE and never exposed.
"""
