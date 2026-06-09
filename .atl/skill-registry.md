# Skill Registry — segal-case-tracker

Generated: 2026-06-08

## Stack Context

- **Language**: Python 3.11+
- **Framework**: FastAPI 0.109+
- **Testing**: pytest 8.0+, pytest-asyncio
- **E2E/Scraping**: Playwright 1.41+
- **Database**: PostgreSQL 16, SQLAlchemy 2.0, Alembic
- **Cache**: Redis 7
- **Quality**: black, ruff

## Compact Rules

### Python/FastAPI

- Use async/await for all I/O operations
- Type hints required on all function signatures
- Dependency injection via FastAPI's `Depends()`
- SQLAlchemy 2.0 style with typed models
- Pydantic v2 for schemas

### Testing

- pytest with `asyncio_mode = "auto"`
- TestClient for API tests with SQLite override
- Fixtures in `tests/conftest.py`
- Scraper tests use Playwright async API

### Project Conventions

- Scraper code in `app/scrapper/pjud/` uses Template Method pattern
- Abstract base class `PJUDBaseScraper` for shared functionality
- Competency-specific scrapers (civil, laboral, penal) inherit from base
- Domain dataclasses: `PJUDCase`, `PJUDMovement`, `PJUDDocument`, `PJUDCaseDetail`

## User Skills (by trigger)

| Skill | Trigger | Path |
|-------|---------|------|
| pytest | Python tests, fixtures, mocking | `~/.config/opencode/skills/pytest/SKILL.md` |
| playwright | E2E tests, Page Objects | `~/.config/opencode/skills/playwright/SKILL.md` |
| typescript | TypeScript patterns | `~/.config/opencode/skills/typescript/SKILL.md` |

## Project Skills

No project-level skills defined.

## Convention Files

- `openspec/` — SDD specs directory (existing)
- No AGENTS.md or .cursorrules found
