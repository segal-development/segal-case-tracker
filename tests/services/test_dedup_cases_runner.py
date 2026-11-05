"""Tests for the batched duplicate-cases runner (T5b2).

The runner is the tool for the real cleanup, so most of these tests pin what it
must REFUSE or NOT DO: dry-run by default, no apply without a backup reference
or a quiescent database, one transaction per batch, stop on the first error.
"""

import importlib.util
import json
import math
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.case import Case
from app.models.case_merge_audit import CaseMergeAudit
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.models.sync_history import SyncHistory
from app.services import dedup_cases_runner as runner
from app.services.case_merge import (
    CHILD_TABLES_WITH_CASE_ID,
    CaseMergeResult,
    CaseMergeVerificationError,
    count_duplicate_identity_groups,
    run_case_merge,
)
from app.services.dedup_cases_runner import RunnerRefused, check_integrity, run_batches
from tests.services.test_case_merge_batches import (
    FIRM_RUT,
    _build_groups,
    _count_statements,
    _identity,
    _rol,
    _signature,
)

OLD = datetime.utcnow() - timedelta(days=3)


@pytest.fixture(autouse=True)
def firm(db):
    lawyer = Lawyer(rut=FIRM_RUT, name="Firm Lawyer")
    db.add(lawyer)
    db.commit()
    return lawyer


@pytest.fixture
def factory(db):
    return sessionmaker(bind=db.get_bind(), autoflush=False)


def _seed(db, n, court_code="B-A"):
    """N duplicate groups, committed, with all activity timestamps in the past."""
    court, groups = _build_groups(db, [_rol(i) for i in range(n)], court_code=court_code)
    db.query(Movement).update({Movement.created_at: OLD})
    db.commit()
    return court, groups


def _run(factory, **kwargs):
    defaults = dict(
        batch_size=2,
        apply=True,
        backup_ref="backup-1",
        require_quiescent=False,
        log=lambda *_: None,
    )
    defaults.update(kwargs)
    return run_batches(factory, **defaults)


def _cases(db):
    db.expire_all()
    return db.query(Case).count()


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class TestDryRun:
    def test_is_the_default_and_writes_nothing(self, db, factory, monkeypatch):
        _seed(db, 5)

        def boom(*_a, **_k):
            raise AssertionError("dry-run must never call run_case_merge")

        monkeypatch.setattr(runner, "run_case_merge", boom)

        summary = run_batches(factory, batch_size=2, log=lambda *_: None)

        assert summary.mode == "dry-run"
        assert summary.stopped_because == "dry_run"
        assert summary.batches_done == 0
        assert summary.groups_merged == 0
        assert summary.plan["groups"] == 5
        assert summary.plan["batches_needed"] == 3
        assert len(summary.plan["first_batch_keys"]) == 2
        assert _cases(db) == 10
        assert db.query(CaseMergeAudit).count() == 0

    def test_first_batch_keys_follow_the_merge_ordering(self, db, factory):
        court, _ = _seed(db, 4)

        summary = run_batches(factory, batch_size=2, log=lambda *_: None)
        merged = run_case_merge(db, group_limit=2)

        assert [tuple(k) for k in summary.plan["first_batch_keys"]] == merged.group_keys
        assert merged.group_keys[0] == _identity(court, _rol(0))

    def test_only_issues_selects(self, db, factory):
        _seed(db, 3)
        seen = []
        engine = db.get_bind()

        def _before(conn, cursor, statement, *_):
            seen.append(statement.lstrip().upper())

        event.listen(engine, "before_cursor_execute", _before)
        try:
            run_batches(factory, batch_size=2, log=lambda *_: None)
        finally:
            event.remove(engine, "before_cursor_execute", _before)

        assert seen
        assert all(s.startswith("SELECT") for s in seen)

    def test_summary_is_json_serializable(self, db, factory):
        _seed(db, 2)

        summary = run_batches(factory, batch_size=1, log=lambda *_: None)

        assert json.loads(json.dumps(summary.to_dict()))["mode"] == "dry-run"


class TestApplyGuards:
    @pytest.mark.parametrize("backup_ref", [None, "", "   "])
    def test_apply_without_backup_ref_refuses_and_writes_nothing(
        self, db, factory, monkeypatch, backup_ref
    ):
        _seed(db, 3)
        monkeypatch.setattr(runner, "run_case_merge", lambda *a, **k: pytest.fail("merged"))

        with pytest.raises(RunnerRefused, match="backup"):
            _run(factory, backup_ref=backup_ref)

        assert _cases(db) == 6

    def test_recent_movement_refuses_when_quiescence_is_required(self, db, factory, monkeypatch):
        _seed(db, 3)
        db.query(Movement).filter(Movement.folio == "1").limit(1)
        first = db.query(Movement).first()
        first.created_at = datetime.utcnow() - timedelta(seconds=30)
        db.commit()
        monkeypatch.setattr(runner, "run_case_merge", lambda *a, **k: pytest.fail("merged"))

        with pytest.raises(RunnerRefused, match="movements"):
            _run(factory, require_quiescent=True, quiescence_s=600)

        assert _cases(db) == 6

    @pytest.mark.parametrize("column", ["started_at", "completed_at"])
    def test_recent_sync_history_refuses(self, db, factory, monkeypatch, column):
        _seed(db, 3)
        firm = db.query(Lawyer).first()
        row = SyncHistory(
            lawyer_id=firm.id,
            competencia="civil",
            started_at=OLD,
            completed_at=OLD,
        )
        setattr(row, column, datetime.utcnow() - timedelta(seconds=30))
        db.add(row)
        db.commit()
        monkeypatch.setattr(runner, "run_case_merge", lambda *a, **k: pytest.fail("merged"))

        with pytest.raises(RunnerRefused, match="sync_history"):
            _run(factory, require_quiescent=True, quiescence_s=600)

        assert _cases(db) == 6

    def test_quiescent_database_proceeds(self, db, factory):
        _seed(db, 3)
        db.add(SyncHistory(lawyer_id=db.query(Lawyer).first().id, competencia="civil",
                           started_at=OLD, completed_at=OLD))
        db.commit()

        summary = _run(factory, require_quiescent=True, quiescence_s=600)

        assert summary.stopped_because == "completed"
        assert _cases(db) == 3

    def test_empty_tables_count_as_quiescent(self, db, factory):
        summary = _run(factory, require_quiescent=True)

        assert summary.stopped_because == "completed"
        assert summary.batches_done == 0

    def test_quiescence_check_is_two_aggregate_queries(self, db, factory):
        _seed(db, 3)
        with factory() as session:
            statements = _count_statements(session, lambda: runner.assert_quiescent(session, 600))

        assert statements == 2


class TestBatching:
    @pytest.mark.parametrize("n,k", [(7, 3), (6, 3), (4, 10), (5, 1)])
    def test_runs_ceil_n_over_k_batches_and_ends_clean(self, db, factory, n, k):
        _seed(db, n)

        summary = _run(factory, batch_size=k)

        assert summary.batches_done == math.ceil(n / k)
        assert summary.groups_merged == n
        assert summary.losers_deleted == n
        assert summary.remaining_groups == 0
        assert summary.stopped_because == "completed"
        assert summary.groups_before == n
        assert _cases(db) == n

    def test_final_state_equals_one_full_run_case_merge(self, db, factory):
        _seed(db, 5)
        _run(factory, batch_size=2)
        db.expire_all()
        batched = _signature(db)
        db.expunge_all()

        Base.metadata.drop_all(bind=db.get_bind())
        Base.metadata.create_all(bind=db.get_bind())
        db.expire_all()
        db.add(Lawyer(rut=FIRM_RUT, name="Firm Lawyer"))
        db.flush()
        _build_groups(db, [_rol(i) for i in range(5)])
        run_case_merge(db)
        db.commit()

        assert _signature(db) == batched

    def test_max_batches_stops_early_and_a_second_call_resumes(self, db, factory):
        _seed(db, 5)

        first = _run(factory, batch_size=1, max_batches=2)
        second = _run(factory, batch_size=1)

        assert first.stopped_because == "max_batches"
        assert first.batches_done == 2
        assert first.remaining_groups == 3
        assert second.stopped_because == "completed"
        assert second.batches_done == 3
        keys_first = {tuple(k) for b in first.batches for k in b["group_keys"]}
        keys_second = {tuple(k) for b in second.batches for k in b["group_keys"]}
        assert len(keys_first) == 2 and len(keys_second) == 3
        assert keys_first.isdisjoint(keys_second)
        assert db.query(CaseMergeAudit).count() == 5
        assert _cases(db) == 5

    def test_stop_file_stops_between_batches(self, db, factory, monkeypatch, tmp_path):
        _seed(db, 5)
        stop = tmp_path / "STOP"
        calls = []

        def merge_then_request_stop(session, **kwargs):
            result = run_case_merge(session, **kwargs)
            calls.append(1)
            stop.write_text("")
            return result

        monkeypatch.setattr(runner, "run_case_merge", merge_then_request_stop)

        summary = _run(factory, batch_size=1, stop_file=str(stop))

        assert summary.stopped_because == "stop_file"
        assert summary.batches_done == 1
        assert len(calls) == 1
        assert _cases(db) == 9

    def test_existing_stop_file_prevents_the_first_batch(self, db, factory, tmp_path):
        _seed(db, 3)
        stop = tmp_path / "STOP"
        stop.write_text("")

        summary = _run(factory, stop_file=str(stop))

        assert summary.stopped_because == "stop_file"
        assert summary.batches_done == 0
        assert _cases(db) == 6

    def test_time_budget_stops_between_batches_never_mid_batch(self, db, factory, monkeypatch):
        _seed(db, 5)
        clock = FakeClock()

        def slow_merge(session, **kwargs):
            result = run_case_merge(session, **kwargs)
            clock.now += 10
            return result

        monkeypatch.setattr(runner, "_monotonic", clock)
        monkeypatch.setattr(runner, "run_case_merge", slow_merge)

        summary = _run(factory, batch_size=1, time_budget_s=15)

        assert summary.stopped_because == "time_budget"
        assert summary.batches_done == 2
        assert summary.remaining_groups == 3
        assert _cases(db) == 8

    def test_failed_batch_rolls_back_only_itself_and_stops(self, db, factory, monkeypatch):
        _seed(db, 5)
        calls = []

        def fail_second(session, **kwargs):
            calls.append(1)
            result = run_case_merge(session, **kwargs)
            if len(calls) == 2:
                raise CaseMergeVerificationError("movements: stray rows")
            return result

        monkeypatch.setattr(runner, "run_case_merge", fail_second)

        summary = _run(factory, batch_size=2)

        assert summary.stopped_because == "error"
        assert summary.batches_done == 1
        assert len(calls) == 2
        assert "stray rows" in summary.errors[0]
        assert _cases(db) == 8  # batch 1 (2 groups) committed, batch 2 rolled back
        assert db.query(CaseMergeAudit).count() == 2
        assert count_duplicate_identity_groups(db) == 3

    def test_no_progress_batch_stops_instead_of_looping(self, db, factory, monkeypatch):
        _seed(db, 3)
        calls = []

        def no_progress(session, **kwargs):
            calls.append(1)
            return CaseMergeResult(merged_rol_count=0, remaining_groups=3)

        monkeypatch.setattr(runner, "run_case_merge", no_progress)

        summary = _run(factory, batch_size=2)

        assert summary.stopped_because == "no_progress"
        assert len(calls) == 1
        assert summary.remaining_groups == 3

    def test_summary_reports_timings_and_average_per_group(self, db, factory, monkeypatch):
        _seed(db, 4)
        clock = FakeClock()

        def merge(session, **kwargs):
            result = run_case_merge(session, **kwargs)
            clock.now += 4
            return result

        monkeypatch.setattr(runner, "_monotonic", clock)
        monkeypatch.setattr(runner, "run_case_merge", merge)

        summary = _run(factory, batch_size=2)

        assert [b["seconds"] for b in summary.batches] == [4, 4]
        assert summary.avg_seconds_per_group == 2
        assert summary.started_at and summary.ended_at
        assert json.loads(json.dumps(summary.to_dict()))["batches_done"] == 2

    def test_logs_one_line_per_batch(self, db, factory):
        _seed(db, 3)
        lines = []

        _run(factory, batch_size=2, log=lines.append)

        batch_lines = [line for line in lines if line.startswith("lote ")]
        assert len(batch_lines) == 2
        assert "restantes=1" in batch_lines[0]


class TestIntegrityCheck:
    def test_clean_database_reports_zero_for_every_table(self, db, factory):
        _seed(db, 2)
        with factory() as session:
            result = check_integrity(session)

        assert set(result) == {m.__tablename__ for m in CHILD_TABLES_WITH_CASE_ID}
        assert all(n == 0 for n in result.values())

    def test_orphan_child_row_is_detected_and_stops_the_run(self, db, factory):
        _seed(db, 4)
        db.add(Movement(case_id=999_999, folio="9", description="dangling",
                        movement_date=OLD, created_at=OLD))
        db.commit()

        summary = _run(factory, batch_size=2, verify_every=1)

        assert summary.stopped_because == "integrity_failed"
        assert summary.batches_done == 1
        assert "movements" in summary.errors[0]
        assert summary.integrity_checks[-1]["ok"] is False

    def test_runs_after_the_last_batch_and_every_n_batches(self, db, factory):
        _seed(db, 5)

        summary = _run(factory, batch_size=1, verify_every=2)

        assert [c["after_batch"] for c in summary.integrity_checks] == [2, 4, 5]
        assert all(c["ok"] for c in summary.integrity_checks)

    def test_statement_count_is_fixed_and_does_not_scale_with_cases(self, db, factory):
        _seed(db, 2)
        with factory() as session:
            few = _count_statements(session, lambda: check_integrity(session))
        _seed(db, 25, court_code="B-Z")
        with factory() as session:
            many = _count_statements(session, lambda: check_integrity(session))

        assert few == many == len(CHILD_TABLES_WITH_CASE_ID)


def _load_cli():
    path = Path(__file__).resolve().parents[2] / "scripts" / "dedup_cases_run.py"
    spec = importlib.util.spec_from_file_location("dedup_cases_run_cli", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestCli:
    @pytest.fixture
    def cli(self, factory, monkeypatch):
        module = _load_cli()
        monkeypatch.setattr(module, "_open_session", factory)
        return module

    def test_batch_size_is_required(self, cli):
        with pytest.raises(SystemExit) as exc:
            cli.main([])
        assert exc.value.code == 2

    def test_apply_without_backup_ref_is_rejected(self, cli, db):
        _seed(db, 2)
        with pytest.raises(SystemExit) as exc:
            cli.main(["--batch-size", "5", "--apply", "--yes"])
        assert exc.value.code == 2
        assert _cases(db) == 4

    def test_default_prints_plan_and_first_apply_command_without_writing(self, cli, db, capsys):
        _seed(db, 3)

        code = cli.main(["--batch-size", "50"])

        out = capsys.readouterr().out
        assert code == 0
        assert "--batch-size 5 --max-batches 1" in out
        assert "--apply" in out and "--backup-ref" in out
        assert _cases(db) == 6

    def test_wrong_confirmation_merges_nothing(self, cli, db, monkeypatch):
        _seed(db, 3)
        monkeypatch.setattr("builtins.input", lambda *_: "merge please")

        code = cli.main(["--batch-size", "2", "--apply", "--backup-ref", "bk",
                         "--i-know-the-worker-is-running"])

        assert code != 0
        assert _cases(db) == 6

    def test_typed_merge_runs_and_writes_json(self, cli, db, monkeypatch, tmp_path, capsys):
        _seed(db, 3)
        monkeypatch.setattr("builtins.input", lambda *_: "MERGE")
        out_file = tmp_path / "summary.json"

        code = cli.main(["--batch-size", "2", "--apply", "--backup-ref", "bk",
                         "--i-know-the-worker-is-running", "--json", str(out_file)])

        assert code == 0
        assert "ADVERTENCIA" in capsys.readouterr().out
        assert _cases(db) == 3
        assert json.loads(out_file.read_text())["stopped_because"] == "completed"

    def test_yes_skips_the_prompt_but_quiescence_still_applies(self, cli, db, monkeypatch, capsys):
        _seed(db, 2)
        db.query(Movement).first().created_at = datetime.utcnow()
        db.commit()
        monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("prompted"))

        code = cli.main(["--batch-size", "2", "--apply", "--backup-ref", "bk", "--yes"])

        assert code != 0
        assert "movements" in capsys.readouterr().out
        assert _cases(db) == 4
