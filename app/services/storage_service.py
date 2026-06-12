"""Storage service — backend-agnostic document storage (Slice 2).

StorageBackend protocol: upload / signed_url / exists / retrieve.
  LocalStorageBackend — dev/test disk storage under DOC_STORAGE_DIR.
  GCSStorageBackend   — production GCS; google-cloud-storage imported lazily
                        (construction does NOT trigger the import — safe in tests).
StorageService        — wraps a backend, enforces idempotency, updates Document row.
get_storage_backend   — factory: returns GCS when settings.GCS_BUCKET is set, else Local.

ADR-4: StorageBackend is a swappable Protocol. GCSStorageBackend uses ADC
(Application Default Credentials) — no key file needed when running in GCP.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.document import Document

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Storage key helper
# ---------------------------------------------------------------------------

def document_storage_key(doc: "Document") -> str:
    """Build a deterministic, content-addressable storage key for a Document row.

    Format: ``cases/{case_id}/{doc_type}_{hash12}.pdf``

    - ``case_id``  — always present on a persisted Document.
    - ``doc_type`` — normalized type string, e.g. "resolution", "cert_envio".
    - ``hash12``   — first 12 chars of pjud_token_hash (ADR-1 stable identity).
    """
    doc_type = doc.doc_type or "unknown"
    hash12 = (doc.pjud_token_hash or "nohash")[:12]
    return f"cases/{doc.case_id}/{doc_type}_{hash12}.pdf"


# ---------------------------------------------------------------------------
# LocalStorageBackend
# ---------------------------------------------------------------------------

class LocalStorageBackend:
    """Disk-based storage backend for development and testing.

    Files are written under *base_dir* using the storage key as a relative path.
    Parent directories are created automatically.  ``signed_url`` returns a
    ``file://`` URL pointing at the absolute path on disk.
    """

    def __init__(self, base_dir: str = "./storage/documents") -> None:
        self._base_dir = Path(base_dir)

    def _path(self, key: str) -> Path:
        return self._base_dir / key

    def upload(self, data: bytes, key: str, content_type: str = "application/pdf") -> str:
        """Write *data* to disk and return the key as the storage URI."""
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        logger.debug("LocalStorageBackend: wrote %d bytes → %s", len(data), target)
        return key  # URI = relative key; retrieve() uses it against the same base_dir

    def signed_url(self, key: str, expires_s: int = 3600) -> str:
        """Return a file:// URL for the stored file (dev/test only)."""
        abs_path = self._path(key).resolve()
        return f"file://{abs_path}"

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def retrieve(self, key: str) -> bytes:
        """Read and return the raw bytes for the given key."""
        return self._path(key).read_bytes()


# ---------------------------------------------------------------------------
# GCSStorageBackend
# ---------------------------------------------------------------------------

class GCSStorageBackend:
    """Google Cloud Storage backend for production use.

    ``google.cloud.storage`` is imported lazily — constructing this class does
    NOT require the library to be installed.  The import is deferred to the
    first call that needs the GCS client (upload / exists / signed_url / retrieve).

    Credentials: Application Default Credentials (ADC).  In GCP environments the
    metadata server supplies them automatically; locally run
    ``gcloud auth application-default login``.
    """

    def __init__(self, bucket_name: str) -> None:
        self._bucket_name = bucket_name
        self._client = None  # Lazy — do NOT import here

    def _get_client(self):
        if self._client is None:
            import google.cloud.storage  # noqa: PLC0415 — intentional lazy import
            self._client = google.cloud.storage.Client()
        return self._client

    def _get_bucket(self):
        return self._get_client().bucket(self._bucket_name)

    def upload(self, data: bytes, key: str, content_type: str = "application/pdf") -> str:
        """Upload *data* to GCS and return a ``gs://`` URI."""
        blob = self._get_bucket().blob(key)
        blob.upload_from_string(data, content_type=content_type)
        uri = f"gs://{self._bucket_name}/{key}"
        logger.info("GCSStorageBackend: uploaded %d bytes → %s", len(data), uri)
        return uri

    def signed_url(self, key: str, expires_s: int = 3600) -> str:
        """Generate a time-limited signed URL for the stored object."""
        from datetime import timedelta
        blob = self._get_bucket().blob(key)
        return blob.generate_signed_url(expiration=timedelta(seconds=expires_s))

    def exists(self, key: str) -> bool:
        return self._get_bucket().blob(key).exists()

    def retrieve(self, uri: str) -> bytes:
        """Download bytes from a ``gs://`` URI."""
        # Strip the gs://{bucket}/ prefix to get the object key.
        prefix = f"gs://{self._bucket_name}/"
        key = uri[len(prefix):] if uri.startswith(prefix) else uri
        blob = self._get_bucket().blob(key)
        return blob.download_as_bytes()


# ---------------------------------------------------------------------------
# StorageService — idempotency + Document model update
# ---------------------------------------------------------------------------

class StorageService:
    """Wrap a StorageBackend with idempotency enforcement and Document ORM updates.

    Idempotency rule (ADR-4): ``upload`` is safe to call repeatedly.
    If ``backend.exists(key)`` returns True, the upload is skipped.  This
    ensures that a re-sync never re-downloads already-stored documents.
    """

    def __init__(self, backend: "LocalStorageBackend | GCSStorageBackend") -> None:
        self._backend = backend

    @property
    def backend(self):
        return self._backend

    def upload(
        self,
        doc: "Document",
        data: bytes,
        content_type: str = "application/pdf",
    ) -> None:
        """Store *data* and update ``doc.gcs_path`` + ``doc.status``.

        Does NOT commit — the caller owns the surrounding transaction.
        Skips silently if the key already exists in the backend.
        """
        key = document_storage_key(doc)
        if self._backend.exists(key):
            logger.debug("StorageService: key already exists, skipping (key=%s)", key)
            return
        uri = self._backend.upload(data, key, content_type)
        doc.gcs_path = uri
        doc.status = "stored"

    def retrieve(self, storage_uri: str) -> bytes:
        """Return raw bytes for the stored document.

        - LocalStorageBackend: *storage_uri* is the relative key stored in
          ``doc.gcs_path``; the backend reads the corresponding file.
        - GCSStorageBackend:   *storage_uri* is a ``gs://`` URI; the backend
          downloads the object.
        """
        return self._backend.retrieve(storage_uri)

    def signed_url(self, doc: "Document", ttl: int) -> Optional[str]:
        """Return a time-limited URL for *doc*, or ``None`` when not stored."""
        if not doc.gcs_path:
            return None
        key = document_storage_key(doc)
        return self._backend.signed_url(key, ttl)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_storage_backend(settings=None) -> "LocalStorageBackend | GCSStorageBackend":
    """Return the appropriate backend driven by settings.

    - ``settings.GCS_BUCKET`` non-empty → ``GCSStorageBackend``
      (construction does not import google.cloud.storage).
    - otherwise → ``LocalStorageBackend`` using ``settings.DOC_STORAGE_DIR``.
    """
    if settings is None:
        from app.config import settings as _settings
        settings = _settings

    if settings.GCS_BUCKET:
        return GCSStorageBackend(settings.GCS_BUCKET)
    return LocalStorageBackend(settings.DOC_STORAGE_DIR)
