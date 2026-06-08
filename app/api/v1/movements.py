"""Movements endpoints - accessed via /cases/{id}/movements."""

# Note: Movement endpoints are nested under cases in cases.py
# This file is for any standalone movement utilities if needed

from fastapi import APIRouter

router = APIRouter()


# Movement endpoints are primarily accessed via:
# GET /cases/{case_id}/movements - in cases.py
