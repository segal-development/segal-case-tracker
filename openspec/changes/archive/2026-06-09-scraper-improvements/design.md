# Design: Scraper Architecture Improvements

## Technical Approach

Replace the persistent browser + in-memory `_sessions` model with fresh browser per API request. Sessions are restored from Redis cookies. Background worker retains persistent browser (single-process safe). Clave Unica adds an alternative auth flow that bypasses captcha entirely.

## Architecture Decisions

| Decision | Options | Tradeoffs | Choice |
|----------|---------|-----------|--------|
| Browser lifecycle | Persistent vs Fresh-per-request vs Pool | Persistent: fast but stale refs. Fresh: 2-3s overhead but stateless. Pool: complex, overkill | **Fresh-per-request** for API; Persistent for worker |
| Browser factory location | Endpoint vs Service vs Base scraper | Endpoint: tight coupling. Service: indirection. Base: natural fit | **Base scraper** via context manager |
| Session data transfer | Redis cookies vs JWT state vs DB blob | Redis: existing infra, TTL built-in. JWT: complex. DB: slow | **Redis cookies** (existing `session_store.py`) |
| Clave Unica credentials | Encrypt in DB vs Vault vs Redis | DB: simple, existing encryption. Vault: overkill. Redis: volatile | **DB encrypted field** on Lawyer model |
| Auth method detection | User preference vs Auto-detect vs Dual endpoints | Preference: explicit. Auto: fragile. Dual: cleanest API | **Dual endpoints** + user preference flag |

## Data Flow

### PR1: Fresh Browser Flow (API Request)

```
┌─────────────┐     ┌──────────────────┐     ┌───────────────┐
│ API Request │────▶│ pjud.py endpoint │────▶│ BrowserFactory│
└─────────────┘     └──────────────────┘     └───────┬───────┘
                                                     │ create
                                                     ▼
┌─────────────┐     ┌──────────────────┐     ┌───────────────┐
│   Response  │◀────│   Scraper ops    │◀────│  Fresh Page   │
└─────────────┘     └──────────────────┘     └───────┬───────┘
                                                     │ close
                                                     ▼
                                             ┌───────────────┐
                                             │ Browser closed│
                                             └───────────────┘
```

### PR2: Clave Unica Auth Flow

```
┌─────────────┐     ┌──────────────────┐     ┌───────────────────┐
│ /login/cu   │────▶│ ClaveUnicaAuth   │────▶│ PJUD Clave Unica  │
│ (rut, pass) │     │ Module           │     │ Login Form        │
└─────────────┘     └──────────────────┘     └─────────┬─────────┘
                                                       │ redirect
                                                       ▼
┌─────────────┐     ┌──────────────────┐     ┌───────────────────┐
│ Session ID  │◀────│ Redis store      │◀────│ Session cookies   │
└─────────────┘     └──────────────────┘     └───────────────────┘
```

## File Changes

| File | Action | Description |
|------|--------|-------------|
| `app/scrapper/pjud/base.py` | Modify | Add `BrowserFactory` context manager, remove page reuse in `_get_page()` |
| `app/scrapper/pjud/browser.py` | Create | `BrowserFactory` class with `async with` support |
| `app/api/v1/pjud.py` | Modify | Remove `_sessions` dict, use `BrowserFactory` per request |
| `app/scrapper/pjud/clave_unica.py` | Create | Clave Unica authentication module |
| `app/api/v1/auth.py` | Modify | Add `/login/clave-unica` endpoint |
| `app/models/lawyer.py` | Modify | Add `clave_unica_rut`, `encrypted_clave_unica_pass`, `auth_method` fields |
| `app/services/session_store.py` | Modify | Add `auth_method` to `PJUDSession` dataclass |
| `app/config.py` | Modify | Add `SCRAPER_FRESH_BROWSER` feature flag |

## Interfaces / Contracts

### BrowserFactory (PR1)

```python
# app/scrapper/pjud/browser.py

class BrowserFactory:
    """Context manager for fresh browser instances."""
    
    def __init__(self, headless: bool = True):
        self.headless = headless
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
    
    async def __aenter__(self) -> "BrowserFactory":
        """Start browser and return factory."""
        ...
    
    async def __aexit__(self, *args) -> None:
        """Close browser and cleanup."""
        ...
    
    async def new_page(self, session: Optional[PJUDSession] = None) -> Page:
        """Create page with optional session restoration."""
        ...

# Usage in endpoints:
async def get_cases(session_id: str):
    session = store.get_session(session_id)
    async with BrowserFactory() as factory:
        page = await factory.new_page(session)
        scraper = CivilScraper(page=page)  # Inject page
        return await scraper.get_my_cases()
```

### ClaveUnicaAuth (PR2)

```python
# app/scrapper/pjud/clave_unica.py

@dataclass
class ClaveUnicaCredentials:
    rut: str  # Clave Unica RUT (may differ from PJUD RUT)
    password: str  # Encrypted, decrypt before use

class ClaveUnicaAuth:
    """Handle Clave Unica authentication flow."""
    
    async def login(
        self,
        page: Page,
        credentials: ClaveUnicaCredentials,
    ) -> PJUDSession:
        """
        Login via Clave Unica portal.
        
        Flow:
        1. Navigate to PJUD login
        2. Click "Clave Unica" button
        3. Fill RUT/password on Clave Unica portal
        4. Handle redirect back to PJUD
        5. Extract session cookies
        """
        ...
```

### Updated Lawyer Model (PR2)

```python
# app/models/lawyer.py additions

class AuthMethod(str, Enum):
    CAPTCHA = "captcha"
    CLAVE_UNICA = "clave_unica"

# New fields:
clave_unica_rut = Column(String(12), nullable=True)  # May differ from PJUD RUT
encrypted_clave_unica_password = Column(String(512), nullable=True)
preferred_auth_method = Column(String(20), default="captcha")
```

## Testing Strategy

| Layer | What to Test | Approach |
|-------|-------------|----------|
| Unit | `BrowserFactory` lifecycle | Mock playwright, verify cleanup |
| Unit | `ClaveUnicaAuth` form fill | Mock page, verify selectors |
| Integration | Fresh browser per request | Real browser, verify no state leak |
| Integration | Clave Unica redirect flow | Mock Clave Unica portal response |
| E2E | Full login + scrape cycle | Existing test infra with flag toggle |

## Migration / Rollout

**Feature Flag**: `SCRAPER_FRESH_BROWSER=true` (default in new deployments)

1. Deploy with flag `false` to existing instances
2. Enable on staging, monitor for 24h
3. Enable on production per-instance
4. After 7 days without "Target page closed" errors, remove old code

**Clave Unica**: Opt-in only. Users manually add credentials in settings. No migration needed.

## Open Questions

- [x] Clave Unica portal selectors may change; need YAML-based selector config (same pattern as PJUD selectors)
- [ ] Rate limiting on Clave Unica portal? (Need to test with real account)
