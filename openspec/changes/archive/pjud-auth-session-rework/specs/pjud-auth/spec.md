# PJUD Auth Specification

## Purpose

Authentication contracts for the captcha (`login_with_token`) and Clave Única login paths,
session-refresh semantics, and autonomous worker re-authentication on session expiry.

**Testability boundary**: All requirements MUST be verified with mocked scraper, session
store, and HTTP dependencies. E2E tests against live PJUD are explicitly NOT a testable
requirement (external blockers: reCAPTCHA token, real credentials, Chromium).

## Requirements

| # | Requirement | Strength |
|---|-------------|----------|
| AUTH-01 | `POST /auth/login` calls `login_with_token(rut, password, captcha_token)`, persists session keyed by `lawyer_id`, returns signed JWT | MUST |
| AUTH-02 | `POST /auth/login/clave-unica` calls the Clave Única scraper method, persists session keyed by `lawyer_id`, returns signed JWT | MUST |
| AUTH-03 | No dedicated session-refresh endpoint exists; refresh is a full re-login on the original endpoint | MUST NOT |
| AUTH-04 | Worker detects expired session (~25 min), decrypts stored credentials, re-authenticates without user interaction | MUST |
| AUTH-05 | Clave Única re-auth path is always attempted when credentials are available | MUST |
| AUTH-06 | Captcha re-auth path is attempted only when a 2Captcha API key is configured; otherwise worker skips with a logged reason | MUST |
| AUTH-07 | Worker skips and logs a reason when no stored credentials exist for `lawyer_id` | MUST |

### Requirement: AUTH-01 — Captcha Login

#### Scenario: Successful captcha login

- GIVEN a valid `rut`, `password`, and `captcha_token`
- WHEN `POST /auth/login` is called
- THEN `login_with_token(rut, password, captcha_token)` is invoked on the scraper
- AND the resulting session is persisted and retrievable by `lawyer_id`
- AND a signed JWT is returned with HTTP 200

#### Scenario: Missing captcha_token field

- GIVEN a request body that omits `captcha_token`
- WHEN `POST /auth/login` is called
- THEN HTTP 422 is returned

#### Scenario: Scraper rejects the captcha token

- GIVEN an invalid or expired `captcha_token`
- WHEN `POST /auth/login` is called
- THEN HTTP 401 is returned

### Requirement: AUTH-02 — Clave Única Login

#### Scenario: Successful Clave Única login

- GIVEN a valid `rut` and `password`
- WHEN `POST /auth/login/clave-unica` is called
- THEN the Clave Única scraper method is invoked
- AND the resulting session is persisted and retrievable by `lawyer_id`
- AND a signed JWT is returned with HTTP 200

#### Scenario: Invalid credentials

- GIVEN an invalid `rut` or incorrect `password`
- WHEN `POST /auth/login/clave-unica` is called
- THEN HTTP 401 is returned

### Requirement: AUTH-04/05 — Worker Re-Authentication (Clave Única)

#### Scenario: Re-auth via Clave Única — nominal

- GIVEN a session for `lawyer_id = N` is expired
- AND Fernet-encrypted Clave Única credentials are stored for `lawyer_id = N`
- WHEN the worker attempts to sync
- THEN credentials are decrypted in memory
- AND the Clave Única login path is called
- AND on success, a new valid session is created and scraping proceeds

### Requirement: AUTH-06 — Worker Re-Authentication (Captcha)

#### Scenario: Captcha re-auth with 2Captcha key configured

- GIVEN a session is expired and 2Captcha API key is configured
- WHEN the worker attempts the captcha re-auth path
- THEN a captcha token is obtained and `login_with_token` is called
- AND on success, a new valid session is created and scraping proceeds

#### Scenario: Captcha re-auth without 2Captcha key

- GIVEN a session is expired and no 2Captcha API key is configured
- WHEN the worker attempts the captcha re-auth path
- THEN the worker skips sync for this lawyer
- AND logs a reason: 2Captcha is not configured

### Requirement: AUTH-07 — No Credentials Available

#### Scenario: No stored credentials for re-auth

- GIVEN a session for `lawyer_id = N` is expired
- AND no encrypted credentials are stored for `lawyer_id = N`
- WHEN the worker attempts to sync
- THEN the worker skips sync for this lawyer
- AND logs a reason: no stored credentials available
