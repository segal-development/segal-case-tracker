"""Tests for app.api.deps.require_ingest_key.

The dependency validates the ``X-Ingest-Key`` header against hashed
IngestKey rows: missing header -> 401, invalid/revoked key -> 403, valid key
-> passes through and stamps ``last_used_at``.
"""

import hashlib

import pytest
from fastapi import HTTPException

from app.api.deps import require_ingest_key
from app.models.ingest_key import IngestKey


def _make_key(db, plaintext: str, is_active: bool = True) -> IngestKey:
    key_hash = hashlib.sha256(plaintext.encode()).hexdigest()
    key = IngestKey(label="operator-test", key_hash=key_hash, is_active=is_active)
    db.add(key)
    db.commit()
    db.refresh(key)
    return key


class TestRequireIngestKey:
    def test_missing_header_raises_401(self, db):
        with pytest.raises(HTTPException) as exc_info:
            require_ingest_key(x_ingest_key=None, db=db)
        assert exc_info.value.status_code == 401

    def test_unknown_key_raises_403(self, db):
        _make_key(db, "the-real-key")
        with pytest.raises(HTTPException) as exc_info:
            require_ingest_key(x_ingest_key="totally-wrong-key", db=db)
        assert exc_info.value.status_code == 403

    def test_revoked_key_raises_403(self, db):
        _make_key(db, "revoked-key", is_active=False)
        with pytest.raises(HTTPException) as exc_info:
            require_ingest_key(x_ingest_key="revoked-key", db=db)
        assert exc_info.value.status_code == 403

    def test_valid_key_passes_and_stamps_last_used_at(self, db):
        key = _make_key(db, "a-valid-key")
        assert key.last_used_at is None

        result = require_ingest_key(x_ingest_key="a-valid-key", db=db)

        assert result.id == key.id
        db.refresh(key)
        assert key.last_used_at is not None

    def test_new_key_after_rotation_works(self, db):
        old_key = _make_key(db, "old-key")
        old_key.is_active = False
        db.commit()
        _make_key(db, "new-key")

        with pytest.raises(HTTPException):
            require_ingest_key(x_ingest_key="old-key", db=db)

        # New key still works.
        result = require_ingest_key(x_ingest_key="new-key", db=db)
        assert result.key_hash == hashlib.sha256(b"new-key").hexdigest()
