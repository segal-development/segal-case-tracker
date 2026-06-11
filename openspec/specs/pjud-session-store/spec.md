# PJUD Session Store Specification

## Purpose

Defines the unified session model and single session store shared by both the scraper (login)
and the sync worker. A session created at login MUST be retrievable by the worker via `lawyer_id`.

**Testability boundary**: Store interaction MUST be verified with a mocked Redis backend.
Live Redis is not a testable dependency.

## Requirements

| # | Requirement | Strength |
|---|-------------|----------|
| SESS-01 | Exactly one `PJUDSession` dataclass exists in the codebase; fields include `session_id`, `lawyer_id`, `auth_method`, `created_at` (UTC), `expires_at` (UTC) | MUST |
| SESS-02 | A single session store is used by both scraper and worker; no parallel session store implementations exist | MUST NOT |
| SESS-03 | Sessions are stored and retrieved keyed by `lawyer_id` | MUST |
| SESS-04 | All timestamps (`created_at`, `expires_at`) are stored as UTC and compared in UTC | MUST |
| SESS-05 | Store unavailability (Redis unreachable) results in a graceful failure — no unhandled exception | MUST |

### Requirement: SESS-01 — Unified PJUDSession Model

#### Scenario: One dataclass definition

- GIVEN the codebase is inspected for `PJUDSession` class definitions
- WHEN all modules are scanned
- THEN exactly one `PJUDSession` definition exists with fields: `session_id`, `lawyer_id`, `auth_method`, `created_at`, `expires_at`

### Requirement: SESS-03 — Session Findable by lawyer_id

#### Scenario: Worker finds session created at login

- GIVEN a session was persisted for `lawyer_id = N` during the login flow
- WHEN the worker calls `get_session_by_lawyer(lawyer_id=N)`
- THEN the session is returned with matching `session_id` and `auth_method`

#### Scenario: No session exists for lawyer_id

- GIVEN no session has been stored for `lawyer_id = N`
- WHEN the worker calls `get_session_by_lawyer(lawyer_id=N)`
- THEN `None` (or an equivalent empty result) is returned without error

### Requirement: SESS-04 — UTC Timestamp Consistency

#### Scenario: Session persisted with UTC timestamps

- GIVEN a login completes successfully
- WHEN the session is saved to the store
- THEN `created_at` and `expires_at` are UTC-aware datetimes (not local time)

#### Scenario: Expiry check uses UTC comparison

- GIVEN a session with a known `expires_at` in UTC
- WHEN the worker evaluates whether the session is expired
- THEN the comparison is performed against UTC now — no timezone mismatch occurs

### Requirement: SESS-05 — Graceful Store Degradation

#### Scenario: Redis unreachable on session save

- GIVEN the Redis store is unreachable
- WHEN a session save is attempted after login
- THEN the operation fails with a handled error
- AND no unhandled exception propagates to the HTTP layer

#### Scenario: Redis unreachable on session lookup

- GIVEN the Redis store is unreachable
- WHEN the worker attempts `get_session_by_lawyer`
- THEN the operation returns an empty result or raises a handled exception
- AND the worker logs the failure and skips the affected lawyer
