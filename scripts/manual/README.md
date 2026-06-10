# Manual PJUD scripts

Exploratory, run-by-hand scripts for debugging the PJUD portal scraper.

**These are NOT part of the test suite.** They open real Playwright sessions
against the live portal (`oficinajudicialvirtual.pjud.cl`), some prompt for a
captcha token interactively, and they are not network-isolated. Never run them
in CI.

They were renamed from their original `test_*.py` names (the `test_` prefix made
pytest collect them) and moved here out of the repo root. Run one directly:

```bash
.venv/bin/python scripts/manual/pjud_login.py
```

The curated, network-isolated test suite lives in `tests/`.
