"""External read-only API for the Sysgal CRM.

Additive and ISOLATED from the internal app: its own auth (``X-API-Key`` /
``SysgalApiKey``), its own router, mounted separately at ``/api/sysgal/v1`` in
``app.main`` — NOT under ``/api/v1``. Never mutates data.
"""
