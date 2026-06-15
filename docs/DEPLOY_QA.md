# QA Deployment Runbook — GCE + Cloud SQL

Step-by-step guide to deploy Segal Case Tracker to a GCE VM using Cloud SQL Auth Proxy.

---

## Prerequisites

| Tool | Purpose |
|------|---------|
| `gcloud` CLI (authenticated) | GCP resource provisioning |
| `docker` ≥ 24 + `docker compose` v2 | Container runtime |
| Python 3.11 + `cryptography` | Key generation |
| A domain / subdomain pointing at the VM IP | TLS / CORS |

---

## 1. Provision GCP Infrastructure

### 1.1 Cloud SQL (PostgreSQL 16)

```bash
gcloud sql instances create segal-qa \
  --database-version=POSTGRES_16 \
  --tier=db-f1-micro \
  --region=us-central1 \
  --storage-type=SSD \
  --storage-size=10GB

gcloud sql databases create segal_case_tracker_qa \
  --instance=segal-qa

gcloud sql users create segal_qa \
  --instance=segal-qa \
  --password=<DB_PASSWORD>
```

Note the instance connection name (used as `CLOUD_SQL_INSTANCE_CONNECTION_NAME`):
```bash
gcloud sql instances describe segal-qa --format="value(connectionName)"
# e.g. my-project:us-central1:segal-qa
```

### 1.2 GCS Bucket (document storage)

```bash
gsutil mb -p <PROJECT_ID> -l US-CENTRAL1 gs://segal-case-tracker-qa-docs
# Block public access
gsutil uniformbucketlevelaccess set on gs://segal-case-tracker-qa-docs
```

### 1.3 Service Account

```bash
gcloud iam service-accounts create segal-qa-sa \
  --display-name="Segal QA SA"

# Cloud SQL client
gcloud projects add-iam-policy-binding <PROJECT_ID> \
  --member="serviceAccount:segal-qa-sa@<PROJECT_ID>.iam.gserviceaccount.com" \
  --role="roles/cloudsql.client"

# GCS object access
gcloud projects add-iam-policy-binding <PROJECT_ID> \
  --member="serviceAccount:segal-qa-sa@<PROJECT_ID>.iam.gserviceaccount.com" \
  --role="roles/storage.objectAdmin"

# Export key JSON — keep this secret
gcloud iam service-accounts keys create sa-key.json \
  --iam-account=segal-qa-sa@<PROJECT_ID>.iam.gserviceaccount.com
```

### 1.4 GCE VM

```bash
gcloud compute instances create segal-qa-vm \
  --machine-type=e2-standard-2 \
  --zone=us-central1-a \
  --image-family=debian-12 \
  --image-project=debian-cloud \
  --boot-disk-size=30GB \
  --tags=http-server,https-server

# Allow ports 80 and 443
gcloud compute firewall-rules create allow-web \
  --allow=tcp:80,tcp:443 \
  --target-tags=http-server,https-server
```

---

## 2. Install Docker on the VM

```bash
gcloud compute ssh segal-qa-vm --zone=us-central1-a

# On the VM:
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg \
  | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker $USER
# Log out and back in for group membership to take effect
```

---

## 3. Clone the Repository

```bash
# On the VM:
git clone https://github.com/segal-development/segal-case-tracker.git
cd segal-case-tracker
git checkout main   # or the release tag
```

---

## 4. Place the Service Account Key

```bash
mkdir -p /home/deploy/secrets
# Upload sa-key.json from your workstation:
# gcloud compute scp sa-key.json segal-qa-vm:/home/deploy/secrets/sa-key.json --zone=us-central1-a
chmod 600 /home/deploy/secrets/sa-key.json
```

---

## 5. Set Up .env.qa

```bash
cp .env.qa.example .env.qa
# Edit .env.qa with your values:
#   DB_USER, DB_PASSWORD, DB_NAME
#   CLOUD_SQL_INSTANCE_CONNECTION_NAME
#   GOOGLE_APPLICATION_CREDENTIALS_PATH=/home/deploy/secrets/sa-key.json
#   GCS_BUCKET
#   SECRET_KEY   (generate below)
#   ENCRYPTION_KEY  (generate below)
#   CAPTCHA_API_KEY
#   SENDGRID_API_KEY, SENDGRID_FROM_EMAIL
#   BASE_URL, CORS_ORIGINS
nano .env.qa
```

### Generate SECRET_KEY and ENCRYPTION_KEY

```bash
# SECRET_KEY (random hex string)
python3 -c "import secrets; print(secrets.token_hex(32))"

# ENCRYPTION_KEY (must be a valid Fernet key — URL-safe base64, 32 bytes)
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

**Keep ENCRYPTION_KEY in a secret manager** (e.g., GCP Secret Manager). If you lose it, all stored lawyer PJUD credentials become unreadable.

---

## 6. Run the Stack

```bash
docker compose -f docker-compose.yml -f docker-compose.qa.yml up -d
```

Docker Compose merge behavior:
- `docker-compose.yml` is the base (dev defaults).
- `docker-compose.qa.yml` overrides: disables local `postgres` (via `profiles: dev-only`), adds `cloud-sql-proxy` and `migrate` services, overrides `api`/`worker` with QA env vars.

**Note:** If you see a warning about `postgres` container not meeting health conditions, pass `--no-deps` or ignore it — the `api`/`worker` services use `cloud-sql-proxy:5432` as their database host, not the local postgres container.

---

## 7. Verify Migrations

```bash
# Watch the migrate container to confirm it exits 0
docker compose -f docker-compose.yml -f docker-compose.qa.yml logs migrate

# Expected last lines:
# INFO  [alembic.runtime.migration] Running upgrade ...
# (exits with code 0 — Docker shows "Exited (0)")
docker compose -f docker-compose.yml -f docker-compose.qa.yml ps
```

---

## 8. Seed Lawyer Encrypted PJUD Credentials

Lawyer PJUD credentials are stored as Fernet ciphertext in the database columns:
- `lawyers.encrypted_pjud_password` (for captcha auth method)
- `lawyers.encrypted_clave_unica_password` (for clave_unica auth method)

The app exposes a seed script via the existing auth API. To encrypt and store credentials manually:

```python
# Run on the server (inside the api container or locally with the same ENCRYPTION_KEY)
from cryptography.fernet import Fernet
import base64, os

key = os.environ["ENCRYPTION_KEY"].encode()
try:
    f = Fernet(key)
except Exception:
    key_bytes = key.ljust(32)[:32]
    f = Fernet(base64.urlsafe_b64encode(key_bytes))

plaintext = "the-pjud-password"
ciphertext = f.encrypt(plaintext.encode()).decode()
print(ciphertext)
# UPDATE lawyers SET encrypted_pjud_password = '<ciphertext>' WHERE rut = '12345678-9';
```

Or use the API endpoint (if implemented) to register a lawyer with their credentials via the standard POST /api/v1/auth/lawyer endpoint.

---

## 9. Verify Deployment

```bash
# Health check
curl http://<VM_EXTERNAL_IP>:8000/health
# Expected: {"status": "healthy", "scheduler": "disabled"}  (api container)
#           {"status": "healthy", "scheduler": "running"}   (worker, same port if exposed)

# Check all containers are running
docker compose -f docker-compose.yml -f docker-compose.qa.yml ps

# Watch API logs
docker compose -f docker-compose.yml -f docker-compose.qa.yml logs -f api

# Trigger a manual sync for a lawyer (if manual sync endpoint exists)
# curl -X POST http://<IP>:8000/api/v1/sync/<lawyer_id> -H "Authorization: Bearer <token>"
```

---

## 10. Watch Logs

```bash
# All services
docker compose -f docker-compose.yml -f docker-compose.qa.yml logs -f

# Worker only (shows scheduler activity)
docker compose -f docker-compose.yml -f docker-compose.qa.yml logs -f worker

# Filter for errors
docker compose -f docker-compose.yml -f docker-compose.qa.yml logs worker 2>&1 | grep -i error
```

### Structured JSON Logging for GCP Cloud Logging

The app uses Python's `logging` module with a plain text formatter. To ingest into Cloud Logging with proper severity, install the `google-cloud-logging` package and set up the GCP log handler, or use a sidecar log agent (Ops Agent).

Quick option — add `LOG_FORMAT=json` env var support to `app/main.py` and format logs as:
```json
{"severity": "INFO", "message": "...", "timestamp": "..."}
```

GCP Cloud Logging automatically parses the `severity` field when using the Ops Agent or when logs come from a service running on GCE with the Ops Agent installed.

---

## 11. Updating the Deployment

```bash
git pull origin main
docker compose -f docker-compose.yml -f docker-compose.qa.yml build
docker compose -f docker-compose.yml -f docker-compose.qa.yml up -d
# migrate runs automatically on each up, applying any new alembic revisions
```

---

## Open Risks / Known Gaps

| Risk | Severity | Mitigation |
|------|----------|-----------|
| `ENCRYPTION_KEY` not in secret manager | High | Store in GCP Secret Manager; mount via `--mount=type=secret` or `gcloud secrets versions access` at boot |
| No TLS termination | High | Place Nginx or Cloud Load Balancer in front; use Let's Encrypt |
| `postgres` container in base compose is disabled via profile but still defined | Low | Compose merge leaves the service declaration; it simply doesn't start. `depends_on: postgres` in the base is unreachable but Docker Compose logs a warning rather than failing |
| Worker `shm_size: 2gb` may be unavailable on small VMs | Medium | Use at least `e2-standard-2` (8 GB RAM); reduce `DETAIL_BATCH_SIZE` if Chromium OOMs |
| No GCS signed-URL TTL configured for prod | Low | Set `GCS_SIGNED_URL_TTL=3600` (default) or override in `.env.qa` |
| Captcha re-auth cost | Low | Each session expiry for a captcha-auth lawyer costs ~$0.003 (2Captcha pricing ~$2.99/1000). Monitor 2Captcha balance |
| No structured JSON logging | Medium | Add `google-cloud-logging` and a JSON formatter to reduce parsing overhead in Cloud Logging |
| `migrate` container restart policy is `on-failure` — will retry indefinitely if Cloud SQL is unreachable | Low | Add a timeout or startup probe; monitor via `docker compose ps` |
| `alembic.ini` `sqlalchemy.url` placeholder is overridden at runtime by `alembic/env.py` | Info | Not a bug — `env.py` calls `config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)`, so the placeholder in `alembic.ini` is ignored |
