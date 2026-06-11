# Lawyer Identity Specification

## Purpose

Multi-lawyer resolution via `_get_or_create_lawyer` and Fernet-encrypted credential storage.
Hardcoded `lawyer_id` values (0, 1, or any other constant) are prohibited in auth and session code.

**Testability boundary**: `_get_or_create_lawyer` MUST be verified with a mocked SQLAlchemy
session. Credential encryption/decryption is verified with a test Fernet key.

## Requirements

| # | Requirement | Strength |
|---|-------------|----------|
| LID-01 | `_get_or_create_lawyer(rut)` returns the existing lawyer record for that RUT or creates one; it MUST NOT return `None` | MUST |
| LID-02 | `lawyer_id=0` and `lawyer_id=1` (and any other hardcoded constant) MUST NOT appear in auth or session code | MUST NOT |
| LID-03 | Every persisted session MUST have `lawyer_id` equal to the value returned by `_get_or_create_lawyer` for the authenticated RUT | MUST |
| LID-04 | PJUD credentials are stored Fernet-encrypted; plaintext passwords MUST NOT be persisted to any storage layer | MUST NOT |
| LID-05 | The worker decrypts Fernet-encrypted credentials in memory at re-auth time; decrypted plaintext MUST NOT be written to any persistent storage | MUST |

### Requirement: LID-01 — Lawyer Resolution

#### Scenario: Existing lawyer returned by RUT

- GIVEN a lawyer with `rut = "12345678-9"` already exists in the database
- WHEN `_get_or_create_lawyer("12345678-9")` is called
- THEN the existing lawyer record is returned
- AND no duplicate record is created

#### Scenario: New lawyer created for unknown RUT

- GIVEN no lawyer with `rut = "12345678-9"` exists in the database
- WHEN `_get_or_create_lawyer("12345678-9")` is called
- THEN a new lawyer record is created and returned with a valid database-assigned `id`

### Requirement: LID-02/03 — No Hardcoded lawyer_id

#### Scenario: Session bound to resolved lawyer_id after login

- GIVEN login completes for `rut = "12345678-9"`
- AND `_get_or_create_lawyer("12345678-9")` returns `lawyer_id = N`
- WHEN the session is persisted
- THEN the session's `lawyer_id` field equals `N`
- AND `N` is never a hardcoded constant (0, 1, etc.) unless the database genuinely assigns that value

#### Scenario: No hardcoded fallback in Clave Única path

- GIVEN the Clave Única login handler executes
- WHEN `_get_or_create_lawyer` is called
- THEN the returned `lawyer_id` is used — `lawyer_id=1` is not set as a fallback

### Requirement: LID-04 — Encrypted Credential Storage

#### Scenario: Credentials persisted as ciphertext

- GIVEN a successful login with `password = "<plaintext>"`
- WHEN credentials are saved for the lawyer
- THEN the stored value is Fernet-encrypted ciphertext
- AND the plaintext password is not present in any storage field

### Requirement: LID-05 — In-Memory Decryption at Re-Auth

#### Scenario: Worker decrypts credentials for re-authentication

- GIVEN Fernet-encrypted credentials are stored for `lawyer_id = N`
- WHEN the worker triggers autonomous re-authentication
- THEN credentials are decrypted in memory only
- AND the decrypted plaintext is passed to the login call and then discarded
- AND plaintext is not written to logs, the database, or any cache
