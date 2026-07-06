"""Tests for app.api.deps.firm_lawyer_id.

Centralizes the "resolve FIRM_LAWYER_RUT -> the firm Lawyer row id" pattern
duplicated at deps.py:170 and stats.py:134. Callers must get a clear error
when the firm Lawyer row is missing (misconfigured environment) rather than
a silent None/NULL write.
"""

import pytest

from app.api.deps import firm_lawyer_id
from app.models.lawyer import Lawyer


class TestFirmLawyerId:
    def test_firm_lawyer_id_returns_firm_lawyer_row_id(self, db, monkeypatch):
        monkeypatch.setenv("FIRM_LAWYER_RUT", "16021492-9")
        firm = Lawyer(rut="16021492-9", name="Carla Firm")
        db.add(firm)
        db.commit()
        db.refresh(firm)

        result = firm_lawyer_id(db)

        assert result == firm.id

    def test_firm_lawyer_id_raises_clear_error_when_firm_lawyer_missing(self, db, monkeypatch):
        monkeypatch.setenv("FIRM_LAWYER_RUT", "16021492-9")
        # No Lawyer row seeded for FIRM_LAWYER_RUT.
        with pytest.raises(RuntimeError, match="FIRM_LAWYER_RUT"):
            firm_lawyer_id(db)
