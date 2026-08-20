"""External API for the Presentación (GEDOC escrito-upload) system.

Additive and ISOLATED from the internal app AND from the Sysgal / Redaccion
APIs: its own auth (``X-API-Key`` / ``PresentacionApiKey``), its own router,
mounted separately at ``/api/presentacion/v1`` in ``app.main`` — NOT under
``/api/v1``, ``/api/sysgal/v1``, or ``/api/redaccion/v1``.

Slice 1 is the API contract + persistence ONLY: an external system (GEDOC)
queues a filing (demanda/escrito) to be presented at the PJUD's OJV, and this
module records it and its lifecycle state. There is NO browser automation, NO
PJUD/Playwright calls, and no background worker yet — a created presentación
just sits at ``estado="en_cola"``. Zero writes to any external system.
"""
