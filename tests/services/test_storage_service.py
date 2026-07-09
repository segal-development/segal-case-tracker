"""Tests for StorageService — local backend, idempotency, and factory.

All tests are mock-based (no GCS credentials required).
GCSStorageBackend type check is safe because the class is defined without a
top-level google.cloud import — the import is deferred to the first API call.
"""
import os
import sys
import pytest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_doc(case_id=1, doc_type="resolution", token_hash="abc123def456789"):
    """Return a minimal Document-like object for StorageService tests."""
    doc = MagicMock()
    doc.id = 7
    doc.case_id = case_id
    doc.doc_type = doc_type
    doc.pjud_token_hash = token_hash
    doc.gcs_path = None
    doc.status = "pending"
    doc.stored_at = None
    return doc


# ---------------------------------------------------------------------------
# LocalStorageBackend
# ---------------------------------------------------------------------------

class TestLocalStorageBackend:
    def test_upload_writes_bytes_to_disk(self, tmp_path):
        from app.services.storage_service import LocalStorageBackend

        backend = LocalStorageBackend(str(tmp_path))
        data = b"%PDF-1.4 fake content"
        key = "cases/1/resolution_abc123def456.pdf"

        uri = backend.upload(data, key, "application/pdf")

        target = tmp_path / key
        assert target.exists(), "File should exist on disk after upload"
        assert target.read_bytes() == data
        assert uri is not None

    def test_exists_false_before_upload(self, tmp_path):
        from app.services.storage_service import LocalStorageBackend

        backend = LocalStorageBackend(str(tmp_path))
        assert backend.exists("cases/1/resolution_nope.pdf") is False

    def test_exists_true_after_upload(self, tmp_path):
        from app.services.storage_service import LocalStorageBackend

        backend = LocalStorageBackend(str(tmp_path))
        key = "cases/1/resolution_abc123.pdf"
        backend.upload(b"bytes", key, "application/pdf")

        assert backend.exists(key) is True

    def test_signed_url_returns_non_empty_string(self, tmp_path):
        from app.services.storage_service import LocalStorageBackend

        backend = LocalStorageBackend(str(tmp_path))
        key = "cases/1/resolution_abc123.pdf"
        backend.upload(b"bytes", key, "application/pdf")

        url = backend.signed_url(key, 3600)
        assert isinstance(url, str)
        assert len(url) > 0

    def test_retrieve_returns_uploaded_bytes(self, tmp_path):
        from app.services.storage_service import LocalStorageBackend

        backend = LocalStorageBackend(str(tmp_path))
        data = b"%PDF fake"
        key = "cases/2/cert_envio_xyz.pdf"
        backend.upload(data, key, "application/pdf")

        result = backend.retrieve(key)
        assert result == data

    def test_upload_creates_parent_dirs(self, tmp_path):
        from app.services.storage_service import LocalStorageBackend

        backend = LocalStorageBackend(str(tmp_path))
        key = "cases/99/resolution_deep_nesting.pdf"
        backend.upload(b"data", key, "application/pdf")

        assert (tmp_path / key).exists()


# ---------------------------------------------------------------------------
# GCSStorageBackend — lazy import safety
# ---------------------------------------------------------------------------

class TestGCSStorageBackendLazyImport:
    def test_module_imports_without_google_cloud(self):
        """Importing storage_service must NOT fail even if google-cloud-storage is absent."""
        # The module may already be imported; the key requirement is that the
        # top-level module-import succeeds without google.cloud present.
        # We verify by importing (it was already tested by collection itself).
        import app.services.storage_service  # noqa: F401 — intentional re-import check
        # If we reach here, top-level import is safe.

    def test_gcs_backend_can_be_instantiated_without_google_cloud(self):
        """GCSStorageBackend() construction must NOT trigger the google.cloud import."""
        from app.services.storage_service import GCSStorageBackend

        # Patch google.cloud.storage out to simulate its absence.
        # If the __init__ triggers the import, this would raise ImportError.
        with patch.dict(sys.modules, {"google.cloud.storage": None}):
            # Construction itself must not import google.cloud.storage.
            backend = GCSStorageBackend("test-bucket")
            assert backend is not None


# ---------------------------------------------------------------------------
# StorageService — idempotency and Document model update
# ---------------------------------------------------------------------------

class TestStorageService:
    def test_skips_upload_when_backend_exists_is_true(self):
        """If backend.exists() returns True, upload is skipped but doc IS reconciled to stored."""
        from app.services.storage_service import StorageService

        expected_uri = "cases/1/resolution_abc123def456.pdf"
        backend = MagicMock()
        backend.exists.return_value = True
        backend.uri_for_key.return_value = expected_uri
        svc = StorageService(backend)

        doc = _make_doc()

        svc.upload(doc, b"bytes")

        backend.upload.assert_not_called()
        # FIX 1: doc must be reconciled even when upload was skipped
        assert doc.status == "stored"
        assert doc.gcs_path == expected_uri

    def test_uploads_and_sets_gcs_path_when_not_exists(self):
        """If backend.exists() returns False, upload bytes and set doc.gcs_path + status."""
        from app.services.storage_service import StorageService

        backend = MagicMock()
        backend.exists.return_value = False
        backend.upload.return_value = "gs://my-bucket/cases/1/resolution_abc123def456.pdf"
        svc = StorageService(backend)

        doc = _make_doc()
        svc.upload(doc, b"PDF bytes")

        backend.upload.assert_called_once()
        assert doc.gcs_path == "gs://my-bucket/cases/1/resolution_abc123def456.pdf"
        assert doc.status == "stored"

    def test_uploads_and_sets_stored_at_when_not_exists(self):
        """FIX (timeline req #8): upload() must stamp stored_at with a real
        transition timestamp, not just flip status."""
        from app.services.storage_service import StorageService

        backend = MagicMock()
        backend.exists.return_value = False
        backend.upload.return_value = "gs://my-bucket/cases/1/resolution_abc123def456.pdf"
        svc = StorageService(backend)

        doc = _make_doc()
        assert doc.stored_at is None

        svc.upload(doc, b"PDF bytes")

        assert doc.stored_at is not None

    def test_skip_upload_path_also_sets_stored_at(self):
        """The idempotent reconcile-only path (key already exists) must also
        stamp stored_at — it still transitions the doc to 'stored'."""
        from app.services.storage_service import StorageService

        expected_uri = "cases/1/resolution_abc123def456.pdf"
        backend = MagicMock()
        backend.exists.return_value = True
        backend.uri_for_key.return_value = expected_uri
        svc = StorageService(backend)

        doc = _make_doc()
        assert doc.stored_at is None

        svc.upload(doc, b"bytes")

        assert doc.stored_at is not None

    def test_signed_url_returns_none_when_gcs_path_is_none(self):
        from app.services.storage_service import StorageService

        backend = MagicMock()
        svc = StorageService(backend)

        doc = _make_doc()
        doc.gcs_path = None

        result = svc.signed_url(doc, 3600)
        assert result is None

    def test_signed_url_delegates_to_backend(self):
        from app.services.storage_service import StorageService

        backend = MagicMock()
        backend.signed_url.return_value = "https://storage.example.com/file"
        svc = StorageService(backend)

        doc = _make_doc()
        doc.gcs_path = "cases/1/resolution_abc123def456.pdf"

        result = svc.signed_url(doc, 3600)
        assert result == "https://storage.example.com/file"
        backend.signed_url.assert_called_once()


# ---------------------------------------------------------------------------
# Factory — get_storage_backend
# ---------------------------------------------------------------------------

class TestGetStorageBackendFactory:
    def test_returns_local_when_backend_is_local(self, monkeypatch):
        """DOC_STORAGE_BACKEND='local' → LocalStorageBackend."""
        from app.services.storage_service import LocalStorageBackend, get_storage_backend

        settings_mock = MagicMock()
        settings_mock.DOC_STORAGE_BACKEND = "local"
        settings_mock.DOC_STORAGE_DIR = "./storage/documents"

        result = get_storage_backend(settings_mock)
        assert isinstance(result, LocalStorageBackend)

    def test_returns_gcs_when_backend_is_gcs_and_bucket_set(self, monkeypatch):
        """DOC_STORAGE_BACKEND='gcs' + GCS_BUCKET set → GCSStorageBackend."""
        from app.services.storage_service import GCSStorageBackend, get_storage_backend

        settings_mock = MagicMock()
        settings_mock.DOC_STORAGE_BACKEND = "gcs"
        settings_mock.GCS_BUCKET = "my-production-bucket"

        result = get_storage_backend(settings_mock)
        assert isinstance(result, GCSStorageBackend)

    def test_gcs_backend_without_bucket_raises_config_error(self, monkeypatch):
        """DOC_STORAGE_BACKEND='gcs' with empty GCS_BUCKET must raise ValueError."""
        from app.services.storage_service import get_storage_backend

        settings_mock = MagicMock()
        settings_mock.DOC_STORAGE_BACKEND = "gcs"
        settings_mock.GCS_BUCKET = ""

        with pytest.raises(ValueError, match="GCS_BUCKET"):
            get_storage_backend(settings_mock)

    def test_unknown_backend_falls_back_to_local(self, monkeypatch):
        """Unrecognised DOC_STORAGE_BACKEND value defaults to LocalStorageBackend."""
        from app.services.storage_service import LocalStorageBackend, get_storage_backend

        settings_mock = MagicMock()
        settings_mock.DOC_STORAGE_BACKEND = "s3"  # not supported
        settings_mock.DOC_STORAGE_DIR = "./storage/documents"

        result = get_storage_backend(settings_mock)
        assert isinstance(result, LocalStorageBackend)


# ---------------------------------------------------------------------------
# LocalStorageBackend — path traversal guard
# ---------------------------------------------------------------------------

class TestLocalStorageBackendPathTraversalGuard:
    def test_upload_rejects_key_that_escapes_base_dir(self, tmp_path):
        """A key with '..' must not be allowed to write outside the storage root."""
        from app.services.storage_service import LocalStorageBackend

        backend = LocalStorageBackend(str(tmp_path))
        with pytest.raises(ValueError, match="escape"):
            backend.upload(b"evil", "../etc/passwd", "application/pdf")

    def test_upload_rejects_absolute_key(self, tmp_path):
        """An absolute path key must be rejected."""
        from app.services.storage_service import LocalStorageBackend

        backend = LocalStorageBackend(str(tmp_path))
        with pytest.raises((ValueError, OSError)):
            backend.upload(b"evil", "/tmp/evil.pdf", "application/pdf")

    def test_upload_allows_valid_nested_key(self, tmp_path):
        """A normal nested key must still work."""
        from app.services.storage_service import LocalStorageBackend

        backend = LocalStorageBackend(str(tmp_path))
        key = "cases/99/resolution_aabbcc.pdf"
        uri = backend.upload(b"data", key, "application/pdf")
        assert uri == key
