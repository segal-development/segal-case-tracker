"""Tests for the IngestKey model (SHA-256 hashed operator API keys)."""

from datetime import datetime

from app.models.ingest_key import IngestKey


class TestIngestKeyModel:
    def test_create_and_persist(self, db):
        key = IngestKey(
            label="operator-carla",
            key_hash="a" * 64,
            is_active=True,
            created_at=datetime.utcnow(),
        )
        db.add(key)
        db.commit()
        db.refresh(key)

        assert key.id is not None
        assert key.label == "operator-carla"
        assert key.key_hash == "a" * 64
        assert key.is_active is True
        assert key.last_used_at is None
        assert key.revoked_at is None

    def test_key_hash_is_unique(self, db):
        from sqlalchemy.exc import IntegrityError
        import pytest

        db.add(IngestKey(label="op-1", key_hash="b" * 64, is_active=True))
        db.commit()

        db.add(IngestKey(label="op-2", key_hash="b" * 64, is_active=True))
        with pytest.raises(IntegrityError):
            db.commit()

    def test_defaults_active_true(self, db):
        key = IngestKey(label="op-3", key_hash="c" * 64)
        db.add(key)
        db.commit()
        db.refresh(key)
        assert key.is_active is True

    def test_revoke_sets_inactive_and_revoked_at(self, db):
        key = IngestKey(label="op-4", key_hash="d" * 64, is_active=True)
        db.add(key)
        db.commit()

        key.is_active = False
        key.revoked_at = datetime.utcnow()
        db.commit()
        db.refresh(key)

        assert key.is_active is False
        assert key.revoked_at is not None
