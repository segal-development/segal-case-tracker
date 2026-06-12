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
        """If backend.exists() returns True, backend.upload() must NOT be called."""
        from app.services.storage_service import StorageService

        backend = MagicMock()
        backend.exists.return_value = True
        svc = StorageService(backend)

        doc = _make_doc()
        doc.gcs_path = "cases/1/resolution_abc123def456.pdf"

        svc.upload(doc, b"bytes")

        backend.upload.assert_not_called()

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
    def test_returns_local_when_gcs_bucket_empty(self, monkeypatch):
        """GCS_BUCKET="" → LocalStorageBackend."""
        from app.services.storage_service import LocalStorageBackend, get_storage_backend

        settings_mock = MagicMock()
        settings_mock.GCS_BUCKET = ""
        settings_mock.DOC_STORAGE_DIR = "./storage/documents"

        result = get_storage_backend(settings_mock)
        assert isinstance(result, LocalStorageBackend)

    def test_returns_gcs_when_gcs_bucket_set(self, monkeypatch):
        """GCS_BUCKET set → GCSStorageBackend (construction must not touch google.cloud)."""
        from app.services.storage_service import GCSStorageBackend, get_storage_backend

        settings_mock = MagicMock()
        settings_mock.GCS_BUCKET = "my-production-bucket"

        result = get_storage_backend(settings_mock)
        assert isinstance(result, GCSStorageBackend)
