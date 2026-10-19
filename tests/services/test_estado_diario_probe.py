"""Estado-diario probe: observe-only measurement of the OJV daily feed.

The probe makes ONE in-page AJAX POST per lawyer per cycle and logs one
greppable line. These tests pin the classification (three outcomes that must
never be conflated), the staleness statistics, the flag, the single request,
and that a probe failure never escapes.
"""
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.services import estado_diario_probe as probe
from app.services.business_days import previous_business_day


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

HEADER = (
    "<table><thead><tr><th>Rol</th><th>Fecha</th><th>Caratulado</th>"
    "<th>Tribunal</th></tr></thead>"
)


def _table(rows: list[tuple[str, str, str, str]]) -> str:
    body = "".join(
        "<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows
    )
    return f"{HEADER}<tbody>{body}</tbody></table>"


def _resp(body: str, status: int = 200) -> dict:
    return {"status": status, "body": body}


LOGIN_PAGE = (
    "<html><body><form action='/home/index.php'>Ingrese su usuario y clave"
    "</form></body></html>"
)
SHAPE_PAGE = (
    "<html><script>TSPD_101</script>failureConfig Support ID: 123 "
    "What code is in the image?</html>"
)


class FakeLimiter:
    def __init__(self) -> None:
        self.acquired = 0

    async def acquire(self) -> None:
        self.acquired += 1


@pytest.fixture
def limiter(monkeypatch) -> FakeLimiter:
    fake = FakeLimiter()
    monkeypatch.setattr(probe, "pjud_action_limiter", lambda action: fake)
    monkeypatch.setattr(probe.asyncio, "sleep", AsyncMock())
    return fake


@pytest.fixture
def log():
    with patch.object(probe, "logger") as mock_logger:
        yield mock_logger


def _logged(mock_logger) -> list[str]:
    out = []
    for level in ("info", "warning", "error"):
        for c in getattr(mock_logger, level).call_args_list:
            out.append(c.args[0] % c.args[1:] if len(c.args) > 1 else c.args[0])
    return out


def _page(response):
    page = MagicMock()
    page.is_closed.return_value = False
    page.goto = AsyncMock()
    if isinstance(response, Exception):
        page.evaluate = AsyncMock(side_effect=response)
    else:
        page.evaluate = AsyncMock(return_value=response)
    return page


def _scraper(page):
    scraper = MagicMock()
    scraper.live_page.return_value = page
    return scraper


def _lawyer(db, rut: str = "11111111-1") -> Lawyer:
    lawyer = db.query(Lawyer).filter(Lawyer.rut == rut).first()
    if lawyer is None:
        lawyer = Lawyer(rut=rut, name=f"L {rut}", is_active=True)
        db.add(lawyer)
        db.flush()
    return lawyer


def _court(db, name: str = "Tribunal") -> Court:
    court = db.query(Court).filter(Court.name == name).first()
    if court is None:
        court = Court(
            code=f"C{db.query(Court).count() + 1}", name=name, region="RM", type="civil"
        )
        db.add(court)
        db.flush()
    return court


def _seed(
    db, rol: str, checked: datetime | None, *,
    lawyer_rut: str = "11111111-1", court_name: str = "Tribunal",
) -> Lawyer:
    lawyer = _lawyer(db, lawyer_rut)
    court = _court(db, court_name)
    db.add(
        Case(
            lawyer_id=lawyer.id, court_id=court.id, rol=rol, competencia="civil",
            plaintiff="A", defendant="B", status="active",
            last_detail_checked_at=checked,
        )
    )
    db.commit()
    return lawyer


def _rows(*pairs):
    """Feed rows as (rol, tribunal-or-None) pairs."""
    return [probe.FeedRow(rol=r, tribunal=t) for r, t in pairs]


def _enable(monkeypatch, value=True):
    monkeypatch.setattr(probe.settings, "ESTADO_DIARIO_PROBE_ENABLED", value)


NOW = datetime(2026, 10, 14, 12, 0, 0)
TODAY = date(2026, 10, 14)  # Wednesday


# ---------------------------------------------------------------------------
# Date choice
# ---------------------------------------------------------------------------

class TestPreviousBusinessDay:
    def test_tuesday_goes_to_monday(self):
        assert previous_business_day(date(2026, 10, 14), include_saturday=False) == date(2026, 10, 13)

    def test_monday_skips_the_weekend_to_friday(self):
        assert previous_business_day(date(2026, 10, 19), include_saturday=False) == date(2026, 10, 16)

    def test_saturday_counts_as_business_day_by_default(self):
        assert previous_business_day(date(2026, 10, 19)) == date(2026, 10, 17)

    def test_skips_a_holiday(self):
        # 2026-10-12 (Monday) is Encuentro de Dos Mundos -> Tuesday looks back to Friday.
        assert previous_business_day(date(2026, 10, 13), include_saturday=False) == date(2026, 10, 9)


# ---------------------------------------------------------------------------
# Classification: the three outcomes must stay distinct
# ---------------------------------------------------------------------------

class TestClassification:
    def test_rows_are_ok_and_rols_extracted(self):
        r = probe.classify_response(_resp(_table([
            ("C-1-2026", "13/10/2026", "A / B", "1 Civil"),
            ("C-2-2026", "13/10/2026", "C / D", "2 Civil"),
        ])))
        assert r.outcome == probe.OK
        assert r.rols == ["C-1-2026", "C-2-2026"]
        assert [row.tribunal for row in r.rows] == ["1 Civil", "2 Civil"]

    def test_empty_tbody_is_no_activity_not_dead(self):
        r = probe.classify_response(_resp(_table([])))
        assert r.outcome == probe.NO_ACTIVITY
        assert r.rols == []

    def test_login_page_is_bounced_not_no_activity(self):
        assert probe.classify_response(_resp(LOGIN_PAGE)).outcome == probe.BOUNCED

    def test_shape_challenge_is_bounced(self):
        assert probe.classify_response(_resp(SHAPE_PAGE)).outcome == probe.BOUNCED

    def test_403_is_bounced(self):
        assert probe.classify_response(_resp("Forbidden", 403)).outcome == probe.BOUNCED

    def test_404_is_endpoint_changed(self):
        assert probe.classify_response(_resp("Not found", 404)).outcome == probe.ENDPOINT_CHANGED

    def test_500_is_endpoint_changed(self):
        assert probe.classify_response(_resp("boom", 500)).outcome == probe.ENDPOINT_CHANGED

    def test_unrelated_html_is_endpoint_changed(self):
        assert probe.classify_response(_resp("<html><p>hola</p></html>")).outcome == probe.ENDPOINT_CHANGED

    def test_empty_body_is_endpoint_changed(self):
        assert probe.classify_response(_resp("")).outcome == probe.ENDPOINT_CHANGED

    def test_table_without_expected_columns_is_endpoint_changed(self):
        html = "<table><thead><tr><th>Foo</th><th>Bar</th></tr></thead><tbody></tbody></table>"
        assert probe.classify_response(_resp(html)).outcome == probe.ENDPOINT_CHANGED

    def test_non_dict_response_is_endpoint_changed(self):
        assert probe.classify_response(None).outcome == probe.ENDPOINT_CHANGED

    def test_rol_column_found_by_header_not_position(self):
        html = (
            "<table><thead><tr><th>Tribunal</th><th>Rol</th><th>Caratulado</th>"
            "<th>Fecha</th></tr></thead><tbody>"
            "<tr><td>1 Civil</td><td>c-9-2026</td><td>A/B</td><td>13/10/2026</td></tr>"
            "</tbody></table>"
        )
        r = probe.classify_response(_resp(html))
        assert r.rols == ["C-9-2026"]
        assert r.rows[0].tribunal == "1 Civil"

    def test_table_without_a_tribunal_column_still_parses_with_none(self):
        """The tribunal column may be absent (other competencia / header):
        that is not an error, the tribunal is just unknown."""
        html = (
            "<table><thead><tr><th>Rol</th><th>Fecha</th><th>Caratulado</th>"
            "</tr></thead><tbody>"
            "<tr><td>c-9-2026</td><td>13/10/2026</td><td>A/B</td></tr>"
            "</tbody></table>"
        )
        r = probe.classify_response(_resp(html))
        assert r.outcome == probe.OK
        assert r.rows == [
            probe.FeedRow(rol="C-9-2026", tribunal=None, caratulado="A/B")
        ]

    def test_blank_tribunal_cell_is_none(self):
        r = probe.classify_response(_resp(_table([("C-1-2026", "13/10/2026", "A/B", "  ")])))
        assert r.rows[0].tribunal is None


# ---------------------------------------------------------------------------
# Staleness statistics
# ---------------------------------------------------------------------------

class TestStats:
    def test_counts_found_missing_median_and_max(self, db):
        lawyer = _seed(db, "C-1-2026", NOW - timedelta(days=2))
        _seed(db, "C-2-2026", NOW - timedelta(days=8))
        _seed(db, "C-3-2026", NOW - timedelta(days=96))
        s = probe.staleness_stats(
            lambda: db,
            _rows(("C-1-2026", None), ("C-2-2026", None), ("C-3-2026", None), ("C-9-2026", None)),
            NOW, lawyer.id,
        )
        assert (s.in_db, s.not_in_db) == (3, 1)
        assert s.median_days == 8.0
        assert s.max_days == 96.0
        assert s.never_checked == 0

    def test_never_checked_cases_are_counted_apart(self, db):
        lawyer = _seed(db, "C-1-2026", None)
        _seed(db, "C-2-2026", NOW - timedelta(days=4))
        s = probe.staleness_stats(
            lambda: db, _rows(("C-1-2026", None), ("C-2-2026", None)), NOW, lawyer.id
        )
        assert (s.in_db, s.never_checked) == (2, 1)
        assert s.median_days == 4.0 and s.max_days == 4.0

    def test_duplicate_rols_in_the_feed_count_once(self, db):
        lawyer = _seed(db, "C-1-2026", NOW - timedelta(days=1))
        s = probe.staleness_stats(
            lambda: db, _rows(("C-1-2026", None), ("C-1-2026", None)), NOW, lawyer.id
        )
        assert (s.in_db, s.not_in_db) == (1, 0)


class TestLawyerScope:
    def test_rol_owned_by_another_lawyer_is_other_lawyer_not_in_db(self, db):
        mine = _seed(db, "C-1-2026", NOW - timedelta(days=1))
        _seed(db, "C-2-2026", NOW - timedelta(days=1), lawyer_rut="22222222-2")
        s = probe.staleness_stats(
            lambda: db, _rows(("C-1-2026", None), ("C-2-2026", None)), NOW, mine.id
        )
        assert s.in_db == 1
        assert s.other_lawyer == 1
        # not_in_db = "not found for this lawyer"; other_lawyer is the subset
        # of it that exists elsewhere in the firm.
        assert s.not_in_db == 1

    def test_rol_nobody_has_is_not_in_db_and_not_other_lawyer(self, db):
        mine = _seed(db, "C-1-2026", NOW - timedelta(days=1))
        s = probe.staleness_stats(
            lambda: db, _rows(("C-1-2026", None), ("C-9-2026", None)), NOW, mine.id
        )
        assert (s.in_db, s.not_in_db, s.other_lawyer) == (1, 1, 0)

    def test_age_is_computed_only_over_this_lawyers_cases(self, db):
        mine = _seed(db, "C-1-2026", NOW - timedelta(days=2))
        # same ROL under another lawyer, far staler: must not leak into the age
        _seed(db, "C-1-2026", NOW - timedelta(days=300), lawyer_rut="22222222-2")
        s = probe.staleness_stats(lambda: db, _rows(("C-1-2026", None)), NOW, mine.id)
        assert s.in_db == 1
        assert s.median_days == 2.0 and s.max_days == 2.0

    def test_never_checked_is_only_over_this_lawyers_cases(self, db):
        mine = _seed(db, "C-1-2026", NOW - timedelta(days=2))
        _seed(db, "C-1-2026", None, lawyer_rut="22222222-2")
        s = probe.staleness_stats(lambda: db, _rows(("C-1-2026", None)), NOW, mine.id)
        assert s.never_checked == 0


class TestCourtComparison:
    def _stats(self, db, lawyer, *pairs):
        return probe.staleness_stats(lambda: db, _rows(*pairs), NOW, lawyer.id)

    def test_same_rol_in_another_court_is_court_mismatch(self, db):
        lawyer = _seed(db, "C-1-2026", NOW, court_name="21º Juzgado Civil de Santiago")
        _court(db, "3º Juzgado Civil de Santiago")  # a known court, but not ours
        s = self._stats(db, lawyer, ("C-1-2026", "3º Juzgado Civil de Santiago"))
        assert s.in_db == 1
        assert (s.court_mismatch, s.court_unknown) == (1, 0)

    def test_matching_court_counts_nothing_extra(self, db):
        lawyer = _seed(db, "C-1-2026", NOW, court_name="21º Juzgado Civil de Santiago")
        s = self._stats(db, lawyer, ("C-1-2026", "21º Juzgado Civil de Santiago"))
        assert (s.in_db, s.court_mismatch, s.court_unknown) == (1, 0, 0)

    def test_ordinal_accent_case_and_spacing_do_not_matter(self, db):
        lawyer = _seed(db, "C-1-2026", NOW, court_name="21º Juzgado Civil de Santiago")
        s = self._stats(db, lawyer, ("C-1-2026", "21 JUZGADO CIVIL DE SANTIAGO"))
        assert (s.court_mismatch, s.court_unknown) == (0, 0)

    def test_degree_sign_and_accents_are_the_same_court(self, db):
        lawyer = _seed(db, "C-1-2026", NOW, court_name="1º Juzgado Civil de Valparaíso")
        s = self._stats(db, lawyer, ("C-1-2026", "  1°  juzgado civil   de VALPARAISO "))
        assert (s.court_mismatch, s.court_unknown) == (0, 0)

    def test_unrecognisable_court_name_is_unknown_never_mismatch(self, db):
        """A false mismatch fabricates an identity bug that does not exist.
        If the feed's name cannot be resolved against courts.name, we count
        court_unknown -- under-count the finding rather than invent it."""
        lawyer = _seed(db, "C-1-2026", NOW, court_name="21º Juzgado Civil de Santiago")
        _court(db, "3º Juzgado Civil de Santiago")
        for garbled in ("21 Civil", "Juzgado de Letras de Quilicura", "???", "Santiago"):
            s = self._stats(db, lawyer, ("C-1-2026", garbled))
            assert (s.court_mismatch, s.court_unknown) == (0, 1), garbled

    def test_unresolvable_does_not_become_mismatch_even_if_names_differ(self, db):
        lawyer = _seed(db, "C-1-2026", NOW, court_name="Tribunal")
        s = self._stats(db, lawyer, ("C-1-2026", "Un tribunal que no existe"))
        assert s.court_mismatch == 0 and s.court_unknown == 1

    def test_row_without_tribunal_is_unknown_and_breaks_nothing(self, db):
        lawyer = _seed(db, "C-1-2026", NOW)
        s = self._stats(db, lawyer, ("C-1-2026", None))
        assert (s.in_db, s.court_mismatch, s.court_unknown) == (1, 0, 1)

    def test_one_certain_mismatch_plus_one_unknown_row_stays_unknown(self, db):
        lawyer = _seed(db, "C-1-2026", NOW, court_name="21º Juzgado Civil de Santiago")
        _court(db, "3º Juzgado Civil de Santiago")
        s = self._stats(
            db, lawyer,
            ("C-1-2026", "3º Juzgado Civil de Santiago"), ("C-1-2026", "garbled"),
        )
        assert (s.court_mismatch, s.court_unknown) == (0, 1)

    def test_a_matching_row_wins_over_a_second_row_in_another_court(self, db):
        lawyer = _seed(db, "C-1-2026", NOW, court_name="21º Juzgado Civil de Santiago")
        _court(db, "3º Juzgado Civil de Santiago")
        s = self._stats(
            db, lawyer,
            ("C-1-2026", "3º Juzgado Civil de Santiago"),
            ("C-1-2026", "21 JUZGADO CIVIL DE SANTIAGO"),
        )
        assert (s.court_mismatch, s.court_unknown) == (0, 0)

    def test_court_is_not_assessed_for_rols_this_lawyer_does_not_have(self, db):
        mine = _seed(db, "C-1-2026", NOW, court_name="21º Juzgado Civil de Santiago")
        _seed(db, "C-2-2026", NOW, lawyer_rut="22222222-2",
              court_name="3º Juzgado Civil de Santiago")
        s = self._stats(db, mine, ("C-2-2026", "3º Juzgado Civil de Santiago"))
        assert (s.in_db, s.other_lawyer) == (0, 1)
        assert (s.court_mismatch, s.court_unknown) == (0, 0)


# ---------------------------------------------------------------------------
# run_estado_diario_probe: flag, single request, log lines, failure isolation
# ---------------------------------------------------------------------------

async def _run(scraper, db, **kw):
    return await probe.run_estado_diario_probe(
        scraper=scraper, lawyer_id=8, competencia="civil",
        today=TODAY, now=NOW, session_factory=lambda: db, **kw,
    )


class TestFlag:
    @pytest.mark.asyncio
    async def test_off_makes_zero_requests(self, monkeypatch, limiter, db):
        _enable(monkeypatch, False)
        page = _page(_resp(_table([])))
        await _run(_scraper(page), db)
        page.evaluate.assert_not_awaited()
        assert limiter.acquired == 0

    def test_default_is_off(self):
        from app.config import Settings
        assert Settings.model_fields["ESTADO_DIARIO_PROBE_ENABLED"].default is False


class TestSingleRequest:
    @pytest.mark.asyncio
    async def test_exactly_one_post_for_the_previous_business_day(
        self, monkeypatch, limiter, db, log
    ):
        _enable(monkeypatch)
        page = _page(_resp(_table([])))
        await _run(_scraper(page), db)
        # Exactly one request to PJUD. Count the calls that actually hit the
        # endpoint, not every `evaluate`: the jQuery feature check is local JS
        # and costs no request, so counting evaluates would be a proxy that
        # breaks every time we add a local check.
        posts = [
            c.args[0] for c in page.evaluate.await_args_list
            if "estadoDiarioFechaCivil.php" in c.args[0]
        ]
        assert len(posts) == 1, f"{len(posts)} requests al endpoint"
        js = posts[0]
        assert "estadoDiarioFechaCivil.php" in js
        assert "fechaEstDiaCiv" in js
        assert "13/10/2026" in js  # Wednesday 14/10 -> Tuesday 13/10
        assert "POST" in js
        assert limiter.acquired == 1

    @pytest.mark.asyncio
    async def test_the_loader_page_is_visited_before_the_post(
        self, monkeypatch, limiter, db, log
    ):
        """The AJAX must be issued from its own page, like the listing is.

        The listing's `$.ajax` works because it runs on the loaded Mis Causas
        panel. Whoever captured this endpoint in 2026-07 recorded
        `GET /miEstadoDiario.php` as its page loader. POSTing cold from
        whatever page the detail phase happened to leave behind risks a
        rejection we would read as "the endpoint is dead" — and this feed has
        already been written off once by mistake. Navigating is free here: the
        browser is torn down between lawyers anyway.
        """
        _enable(monkeypatch)
        page = _page(_resp(_table([])))
        await _run(_scraper(page), db)

        page.goto.assert_awaited_once()
        assert probe.LOADER in page.goto.await_args.args[0]
        # and the loader came FIRST, before the POST
        order = [c[0] for c in page.mock_calls if c[0] in ("goto", "evaluate")]
        assert order[:2] == ["goto", "evaluate"], f"orden real: {order}"

    @pytest.mark.asyncio
    async def test_works_on_a_page_without_jquery(self, monkeypatch, limiter, db, log):
        """Measured in production 2026-10-14, first real run:

            outcome=probe_error error=Page.evaluate: ReferenceError: $ is not defined

        `/miEstadoDiario.php` does not give us jQuery the way the Mis Causas
        panel does, so `$.ajax` is not available. The probe must still issue
        the request — and crucially it must NOT report this as a dead
        endpoint: that is how this feed gets written off by mistake.
        """
        _enable(monkeypatch)
        page = _page(_resp(_table([])))
        # jQuery absent: the jQuery probe evaluates falsey, the request still runs.
        page.evaluate = AsyncMock(side_effect=[False, _resp(_table([]))])

        await _run(_scraper(page), db)

        # it fell back instead of blowing up, and said so honestly
        lines = _logged(log)
        assert lines, "el sondeo no logueo nada"
        joined = " ".join(lines)
        assert "probe_error" not in joined, joined
        assert "outcome=no_activity" in joined, joined

    @pytest.mark.asyncio
    async def test_on_a_monday_the_request_asks_for_the_friday(
        self, monkeypatch, limiter, db, log
    ):
        _enable(monkeypatch)
        page = _page(_resp(_table([])))
        await probe.run_estado_diario_probe(
            scraper=_scraper(page), lawyer_id=8, competencia="civil",
            today=date(2026, 10, 19), now=NOW, session_factory=lambda: db,
        )
        assert "16/10/2026" in page.evaluate.await_args.args[0]

    @pytest.mark.asyncio
    async def test_non_civil_competencia_makes_no_request(
        self, monkeypatch, limiter, db
    ):
        _enable(monkeypatch)
        page = _page(_resp(_table([])))
        await probe.run_estado_diario_probe(
            scraper=_scraper(page), lawyer_id=8, competencia="laboral",
            today=TODAY, now=NOW, session_factory=lambda: db,
        )
        page.evaluate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_live_page_means_no_request_and_no_new_page(
        self, monkeypatch, limiter, db, log
    ):
        _enable(monkeypatch)
        scraper = MagicMock()
        scraper.live_page.return_value = None
        await _run(scraper, db)
        scraper._get_page.assert_not_called()
        scraper._page_for.assert_not_called()
        assert any("outcome=skipped" in l for l in _logged(log))


class TestLogLines:
    @pytest.mark.asyncio
    async def test_ok_line(self, monkeypatch, limiter, db, log):
        _enable(monkeypatch)
        lawyer = _seed(db, "C-1-2026", NOW - timedelta(days=8))
        _seed(db, "C-2-2026", NOW - timedelta(days=96))
        body = _table([
            ("C-1-2026", "13/10/2026", "A/B", "1 Civil"),
            ("C-2-2026", "13/10/2026", "A/B", "1 Civil"),
            ("C-3-2026", "13/10/2026", "A/B", "1 Civil"),
        ])
        await probe.run_estado_diario_probe(
            scraper=_scraper(_page(_resp(body))), lawyer_id=lawyer.id,
            competencia="civil", today=TODAY, now=NOW, session_factory=lambda: db,
        )
        line = _logged(log)[-1]
        assert line.startswith("ESTADO_DIARIO_PROBE ")
        for part in (
            f"lawyer={lawyer.id}", "date=13/10/2026", "outcome=ok", "rows=3", "in_db=2",
            "not_in_db=1", "age_days_median=52.0", "age_days_max=96.0",
        ):
            assert part in line, line

    @pytest.mark.asyncio
    async def test_no_activity_is_info_not_warning(self, monkeypatch, limiter, db, log):
        _enable(monkeypatch)
        await _run(_scraper(_page(_resp(_table([])))), db)
        line = _logged(log)[-1]
        assert "outcome=no_activity" in line and "rows=0" in line
        log.warning.assert_not_called()

    @pytest.mark.asyncio
    async def test_bounce_is_warning_and_says_bounced(self, monkeypatch, limiter, db, log):
        _enable(monkeypatch)
        await _run(_scraper(_page(_resp(LOGIN_PAGE))), db)
        line = _logged(log)[-1]
        assert "outcome=bounced" in line
        assert "no_activity" not in line and "endpoint_changed" not in line
        log.warning.assert_called()

    @pytest.mark.asyncio
    async def test_dead_endpoint_is_warning_and_says_endpoint_changed(
        self, monkeypatch, limiter, db, log
    ):
        _enable(monkeypatch)
        await _run(_scraper(_page(_resp("Not found", 404))), db)
        line = _logged(log)[-1]
        assert "outcome=endpoint_changed" in line and "status=404" in line
        assert "no_activity" not in line
        log.warning.assert_called()


class TestNewLogFields:
    @pytest.mark.asyncio
    async def test_ok_line_carries_the_new_fields_after_the_existing_ones(
        self, monkeypatch, limiter, db, log
    ):
        _enable(monkeypatch)
        lawyer = _seed(db, "C-1-2026", NOW - timedelta(days=8),
                       court_name="21º Juzgado Civil de Santiago")
        _court(db, "3º Juzgado Civil de Santiago")
        _seed(db, "C-2-2026", NOW, lawyer_rut="22222222-2")
        body = _table([
            ("C-1-2026", "13/10/2026", "A/B", "3º Juzgado Civil de Santiago"),
            ("C-2-2026", "13/10/2026", "A/B", "1 Civil"),
            ("C-3-2026", "13/10/2026", "A/B", "1 Civil"),
        ])
        await probe.run_estado_diario_probe(
            scraper=_scraper(_page(_resp(body))), lawyer_id=lawyer.id,
            competencia="civil", today=TODAY, now=NOW, session_factory=lambda: db,
        )
        line = _logged(log)[-1]
        for part in ("in_db=1", "not_in_db=2", "other_lawyer=1",
                     "court_mismatch=1", "court_unknown=0"):
            assert part in line, line
        # existing fields keep their names and order; new ones are appended
        keys = [kv.split("=")[0] for kv in line.split(" ")[1:] if "=" in kv]
        assert keys == [
            "lawyer", "date", "outcome", "rows", "in_db", "not_in_db",
            "never_checked", "age_days_median", "age_days_max",
            "other_lawyer", "court_mismatch", "court_unknown",
        ], keys


class TestSampleLines:
    """A bounded sample of the ROLs behind each counter, on separate lines."""

    async def _go(self, db, log, body, lawyer_id):
        await probe.run_estado_diario_probe(
            scraper=_scraper(_page(_resp(body))), lawyer_id=lawyer_id,
            competencia="civil", today=TODAY, now=NOW, session_factory=lambda: db,
        )
        return [l for l in _logged(log) if l.startswith("ESTADO_DIARIO_PROBE_SAMPLE ")]

    @pytest.mark.asyncio
    async def test_not_in_db_sample_is_capped_at_20_and_counts_the_rest(
        self, monkeypatch, limiter, db, log
    ):
        _enable(monkeypatch)
        lawyer = _seed(db, "C-900-2026", NOW)
        rows = [(f"C-{i:03d}-2026", "13/10/2026", "A/B", "3º Juzgado Civil de Santiago")
                for i in range(25)]
        lines = await self._go(db, log, _table(rows), lawyer.id)
        assert len(lines) == 1, lines
        line = lines[0]
        assert "category=not_in_db" in line
        assert "total=25 shown=20 omitted=5" in line
        assert f"lawyer={lawyer.id}" in line and "date=13/10/2026" in line
        # sorted, first 20 only, with the feed tribunal next to each ROL
        assert "C-000-2026@3º Juzgado Civil de Santiago" in line
        assert "C-019-2026@" in line
        assert "C-020-2026" not in line
        log.warning.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_tribunal_is_shown_as_a_dash(self, monkeypatch, limiter, db, log):
        _enable(monkeypatch)
        lawyer = _seed(db, "C-900-2026", NOW)
        html = (
            "<table><thead><tr><th>Rol</th><th>Caratulado</th></tr></thead><tbody>"
            "<tr><td>C-1-2026</td><td>A/B</td></tr></tbody></table>"
        )
        lines = await self._go(db, log, html, lawyer.id)
        assert "C-1-2026@-" in lines[0]

    @pytest.mark.asyncio
    async def test_each_category_gets_its_own_line(self, monkeypatch, limiter, db, log):
        _enable(monkeypatch)
        lawyer = _seed(db, "C-1-2026", NOW, court_name="21º Juzgado Civil de Santiago")
        _court(db, "3º Juzgado Civil de Santiago")
        _seed(db, "C-2-2026", NOW, lawyer_rut="22222222-2")
        body = _table([
            ("C-1-2026", "13/10/2026", "A/B", "3º Juzgado Civil de Santiago"),
            ("C-2-2026", "13/10/2026", "A/B", "1 Civil"),
            ("C-3-2026", "13/10/2026", "A/B", "1 Civil"),
        ])
        lines = await self._go(db, log, body, lawyer.id)
        by_cat = {l.split("category=")[1].split(" ")[0]: l for l in lines}
        assert set(by_cat) == {"not_in_db", "other_lawyer", "court_mismatch"}
        assert "C-3-2026" in by_cat["not_in_db"] and "C-2-2026" not in by_cat["not_in_db"]
        assert "C-2-2026" in by_cat["other_lawyer"] and "C-3-2026" not in by_cat["other_lawyer"]
        assert "C-1-2026@3º Juzgado Civil de Santiago" in by_cat["court_mismatch"]

    @pytest.mark.asyncio
    async def test_nothing_missing_emits_no_sample_line(self, monkeypatch, limiter, db, log):
        _enable(monkeypatch)
        lawyer = _seed(db, "C-1-2026", NOW)
        lines = await self._go(
            db, log, _table([("C-1-2026", "13/10/2026", "A/B", "Tribunal")]), lawyer.id
        )
        assert lines == []

    @pytest.mark.asyncio
    async def test_the_counters_line_is_untouched_by_the_samples(
        self, monkeypatch, limiter, db, log
    ):
        _enable(monkeypatch)
        lawyer = _seed(db, "C-900-2026", NOW)
        await self._go(db, log, _table([("C-1-2026", "13/10/2026", "A/B", "T")]), lawyer.id)
        counters = [l for l in _logged(log) if l.startswith("ESTADO_DIARIO_PROBE lawyer=")]
        assert len(counters) == 1 and "category=" not in counters[0]


class TestFailureIsolation:
    @pytest.mark.asyncio
    async def test_page_error_never_escapes(self, monkeypatch, limiter, db, log):
        _enable(monkeypatch)
        await _run(_scraper(_page(RuntimeError("Execution context was destroyed"))), db)
        assert "outcome=probe_error" in _logged(log)[-1]

    @pytest.mark.asyncio
    async def test_limiter_timeout_never_escapes(self, monkeypatch, db, log):
        _enable(monkeypatch)

        class Boom:
            async def acquire(self):
                raise TimeoutError("rate limit wait exceeded")

        monkeypatch.setattr(probe, "pjud_action_limiter", lambda a: Boom())
        monkeypatch.setattr(probe.asyncio, "sleep", AsyncMock())
        page = _page(_resp(_table([])))
        await _run(_scraper(page), db)
        page.evaluate.assert_not_awaited()
        assert "outcome=probe_error" in _logged(log)[-1]

    @pytest.mark.asyncio
    async def test_database_error_in_stats_never_escapes(self, monkeypatch, limiter, log):
        _enable(monkeypatch)

        def broken():
            raise RuntimeError("db gone")

        body = _table([("C-1-2026", "13/10/2026", "A/B", "1 Civil")])
        await probe.run_estado_diario_probe(
            scraper=_scraper(_page(_resp(body))), lawyer_id=8, competencia="civil",
            today=TODAY, now=NOW, session_factory=broken,
        )
        assert "outcome=probe_error" in _logged(log)[-1]


class TestReturnsItsRows:
    """The probe hands the feed back to the caller (delta-sync T1)."""

    @pytest.mark.asyncio
    async def test_ok_returns_the_feed_rows_with_their_tribunal(
        self, monkeypatch, limiter, db, log
    ):
        _enable(monkeypatch)
        body = _table([
            ("C-1-2026", "13/10/2026", "A/B", "1 Civil"),
            ("C-2-2026", "13/10/2026", "A/B", "2 Civil"),
        ])
        rows = await _run(_scraper(_page(_resp(body))), db)
        assert rows == [
            probe.FeedRow(rol="C-1-2026", tribunal="1 Civil", caratulado="A/B"),
            probe.FeedRow(rol="C-2-2026", tribunal="2 Civil", caratulado="A/B"),
        ]

    @pytest.mark.asyncio
    async def test_flag_off_returns_no_rows(self, monkeypatch, limiter, db):
        _enable(monkeypatch, False)
        assert await _run(_scraper(_page(_resp(_table([])))), db) == []

    @pytest.mark.asyncio
    async def test_no_activity_returns_no_rows(self, monkeypatch, limiter, db, log):
        _enable(monkeypatch)
        assert await _run(_scraper(_page(_resp(_table([])))), db) == []

    @pytest.mark.asyncio
    async def test_bounced_returns_no_rows(self, monkeypatch, limiter, db, log):
        _enable(monkeypatch)
        assert await _run(_scraper(_page(_resp(SHAPE_PAGE))), db) == []

    @pytest.mark.asyncio
    async def test_internal_failure_returns_no_rows(self, monkeypatch, limiter, db, log):
        _enable(monkeypatch)
        assert await _run(_scraper(_page(RuntimeError("boom"))), db) == []

    @pytest.mark.asyncio
    async def test_stats_failure_after_a_good_response_still_returns_the_rows(
        self, monkeypatch, limiter, db, log
    ):
        """The rows are the product; the log statistics are a by-product."""
        _enable(monkeypatch)
        body = _table([("C-1-2026", "13/10/2026", "A/B", "1 Civil")])

        def broken_factory():
            raise RuntimeError("db down")

        rows = await probe.run_estado_diario_probe(
            scraper=_scraper(_page(_resp(body))), lawyer_id=8, competencia="civil",
            today=TODAY, now=NOW, session_factory=broken_factory,
        )
        assert rows == [
            probe.FeedRow(rol="C-1-2026", tribunal="1 Civil", caratulado="A/B")
        ]


class TestShapeCooldown:
    """While Shape is challenging the station, the probe must not navigate.

    The detail phase already abstains in cooldown (detect_and_sync_movements);
    now that the probe runs BEFORE it, the probe needs the same guard.
    """

    @pytest.fixture
    def cooldown(self):
        from app.services.shape_cooldown import get_shape_cooldown, reset_shape_cooldown

        reset_shape_cooldown()
        get_shape_cooldown().trip()
        yield get_shape_cooldown()
        reset_shape_cooldown()

    @pytest.mark.asyncio
    async def test_active_cooldown_means_no_navigation_no_request_no_rows(
        self, monkeypatch, limiter, db, log, cooldown
    ):
        _enable(monkeypatch)
        page = _page(_resp(_table([("C-1-2026", "13/10/2026", "A/B", "1 Civil")])))
        rows = await _run(_scraper(page), db)

        page.goto.assert_not_awaited()
        page.evaluate.assert_not_awaited()
        assert limiter.acquired == 0
        assert rows == []

    @pytest.mark.asyncio
    async def test_active_cooldown_is_logged_under_the_probe_prefix(
        self, monkeypatch, limiter, db, log, cooldown
    ):
        _enable(monkeypatch)
        await _run(_scraper(_page(_resp(_table([])))), db)
        line = _logged(log)[-1]
        assert line.startswith("ESTADO_DIARIO_PROBE ")
        assert "outcome=skipped" in line
        assert "reason=shape_cooldown" in line

    @pytest.mark.asyncio
    async def test_inactive_cooldown_probes_as_usual(self, monkeypatch, limiter, db, log):
        from app.services.shape_cooldown import reset_shape_cooldown

        reset_shape_cooldown()
        _enable(monkeypatch)
        page = _page(_resp(_table([])))
        await _run(_scraper(page), db)
        page.goto.assert_awaited_once()


# ---------------------------------------------------------------------------
# Range of business days since the last successful read (T4)
# ---------------------------------------------------------------------------
# The probe runs inside sync_lawyer_cases, which the freshness guard can skip
# (measured 2026-10-16: 20 of 25 lawyers skipped in one cycle). Asking only for
# the previous business day meant a skipped cycle lost that day's feed for good.

import re  # noqa: E402

from app.models.lawyer import Lawyer as _Lawyer  # noqa: E402,F401


def _dates_in(js: str) -> str | None:
    m = re.search(r"\d{2}/\d{2}/\d{4}", js)
    return m.group(0) if m else None


def _ddmm(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def _day_page(by_date: dict, default=None, on_post=None):
    """Page whose estado-diario answer depends on the requested date.

    ``by_date`` maps ``dd/mm/YYYY`` -> response dict (or an Exception to raise).
    The local jQuery check answers True and costs no request.
    """
    posted: list[str] = []
    page = MagicMock()
    page.is_closed.return_value = False
    page.goto = AsyncMock()

    async def evaluate(js, *a, **k):
        if "estadoDiarioFechaCivil.php" not in js:
            return True
        fecha = _dates_in(js)
        posted.append(fecha)
        if on_post is not None:
            on_post(fecha)
        resp = by_date.get(fecha, default if default is not None else _resp(_table([])))
        if isinstance(resp, Exception):
            raise resp
        return resp

    page.evaluate = evaluate
    page.posted = posted
    return page


def _watermark(db, lawyer) -> date | None:
    db.expire_all()
    return db.query(Lawyer).filter(Lawyer.id == lawyer.id).one().estado_diario_last_read_on


async def _run_range(scraper, db, lawyer):
    return await probe.run_estado_diario_probe(
        scraper=scraper, lawyer_id=lawyer.id, competencia="civil",
        today=TODAY, now=NOW, session_factory=lambda: db,
    )


# TODAY = Wed 2026-10-14. Mon 12/10 is a feriado (Encuentro de Dos Mundos).
LAST_READ = date(2026, 10, 7)  # Wed
EXPECTED_3 = ["08/10/2026", "09/10/2026", "13/10/2026"]  # skips Sat/Sun and the feriado


class TestBusinessDayRange:
    @pytest.mark.asyncio
    async def test_no_previous_read_asks_for_one_day_like_before(
        self, monkeypatch, limiter, db, log
    ):
        _enable(monkeypatch)
        lawyer = _lawyer(db)
        page = _day_page({})
        await _run_range(_scraper(page), db, lawyer)
        assert page.posted == ["13/10/2026"]

    @pytest.mark.asyncio
    async def test_three_business_days_since_last_read_asks_for_those_three(
        self, monkeypatch, limiter, db, log
    ):
        _enable(monkeypatch)
        lawyer = _lawyer(db)
        lawyer.estado_diario_last_read_on = LAST_READ
        db.commit()
        page = _day_page({})
        await _run_range(_scraper(page), db, lawyer)
        assert page.posted == EXPECTED_3

    @pytest.mark.asyncio
    async def test_success_advances_the_watermark_to_the_last_day_read(
        self, monkeypatch, limiter, db, log
    ):
        _enable(monkeypatch)
        lawyer = _lawyer(db)
        lawyer.estado_diario_last_read_on = LAST_READ
        db.commit()
        await _run_range(_scraper(_day_page({})), db, lawyer)
        assert _watermark(db, lawyer) == date(2026, 10, 13)

    @pytest.mark.asyncio
    async def test_already_up_to_date_still_asks_for_the_latest_day(
        self, monkeypatch, limiter, db, log
    ):
        """The delta-sync re-reads the latest day every cycle until its causas
        are consumed; a watermark must not turn that into zero requests."""
        _enable(monkeypatch)
        lawyer = _lawyer(db)
        lawyer.estado_diario_last_read_on = date(2026, 10, 13)
        db.commit()
        page = _day_page({})
        await _run_range(_scraper(page), db, lawyer)
        assert page.posted == ["13/10/2026"]
        assert _watermark(db, lawyer) == date(2026, 10, 13)

    @pytest.mark.asyncio
    async def test_a_watermark_that_cannot_be_saved_stops_the_range(
        self, monkeypatch, limiter, db, log
    ):
        """A day read but NOT recorded must stop the range.

        Carrying on would record a LATER day as the watermark, and the
        unrecorded one would never be asked again: the exact permanent hole
        this feature exists to close.
        """
        _enable(monkeypatch)
        lawyer = _lawyer(db)
        lawyer.estado_diario_last_read_on = LAST_READ
        db.commit()

        real_save = probe._save_watermark

        def _fails_on_the_first_day(session_factory, lawyer_id, day):
            if day == date(2026, 10, 8):
                raise RuntimeError("database unavailable")
            return real_save(session_factory, lawyer_id, day)

        monkeypatch.setattr(probe, "_save_watermark", _fails_on_the_first_day)
        page = _day_page({})
        await _run_range(_scraper(page), db, lawyer)

        assert page.posted == ["08/10/2026"]  # stopped; 09/10 and 13/10 untouched
        assert _watermark(db, lawyer) == LAST_READ  # unchanged: nothing is skipped

    @pytest.mark.asyncio
    async def test_a_failed_day_is_not_marked_read_and_is_asked_again(
        self, monkeypatch, limiter, db, log
    ):
        _enable(monkeypatch)
        lawyer = _lawyer(db)
        lawyer.estado_diario_last_read_on = LAST_READ
        db.commit()
        # 08/10 ok, 09/10 bounced -> the range stops there
        page = _day_page({"09/10/2026": _resp(LOGIN_PAGE)})
        await _run_range(_scraper(page), db, lawyer)
        assert page.posted == ["08/10/2026", "09/10/2026"]
        assert _watermark(db, lawyer) == date(2026, 10, 8)  # NOT 09/10 nor 13/10

        # next cycle: the failed day (and the ones after it) are requested again
        page2 = _day_page({})
        await _run_range(_scraper(page2), db, lawyer)
        assert page2.posted == ["09/10/2026", "13/10/2026"]
        assert _watermark(db, lawyer) == date(2026, 10, 13)

    @pytest.mark.asyncio
    async def test_a_day_that_raises_is_not_marked_read(
        self, monkeypatch, limiter, db, log
    ):
        _enable(monkeypatch)
        lawyer = _lawyer(db)
        lawyer.estado_diario_last_read_on = LAST_READ
        db.commit()
        page = _day_page({"08/10/2026": RuntimeError("boom")})
        await _run_range(_scraper(page), db, lawyer)
        assert page.posted == ["08/10/2026"]
        assert _watermark(db, lawyer) == LAST_READ

    @pytest.mark.asyncio
    async def test_a_day_with_no_activity_counts_as_read(
        self, monkeypatch, limiter, db, log
    ):
        _enable(monkeypatch)
        lawyer = _lawyer(db)
        db.commit()
        await _run_range(_scraper(_day_page({})), db, lawyer)  # empty tbody
        assert _watermark(db, lawyer) == date(2026, 10, 13)

    @pytest.mark.asyncio
    async def test_cap_limits_requests_for_a_long_gap_oldest_first(
        self, monkeypatch, limiter, db, log
    ):
        _enable(monkeypatch)
        monkeypatch.setattr(probe.settings, "ESTADO_DIARIO_MAX_DAYS_PER_CYCLE", 3)
        lawyer = _lawyer(db)
        lawyer.estado_diario_last_read_on = TODAY - timedelta(days=40)
        db.commit()
        page = _day_page({})
        await _run_range(_scraper(page), db, lawyer)
        assert len(page.posted) == 3
        # oldest first, so the watermark keeps moving contiguously and no
        # day is skipped over
        days = [datetime.strptime(p, "%d/%m/%Y").date() for p in page.posted]
        assert days == sorted(days)
        assert _watermark(db, lawyer) == days[-1]
        assert days[0] > TODAY - timedelta(days=40)

    def test_cap_default_is_conservative_and_env_driven(self):
        from app.config import Settings
        field = Settings.model_fields["ESTADO_DIARIO_MAX_DAYS_PER_CYCLE"]
        assert field.default == 3

    @pytest.mark.asyncio
    async def test_weekends_and_feriados_are_not_requested(
        self, monkeypatch, limiter, db, log
    ):
        _enable(monkeypatch)
        monkeypatch.setattr(probe.settings, "ESTADO_DIARIO_MAX_DAYS_PER_CYCLE", 30)
        lawyer = _lawyer(db)
        lawyer.estado_diario_last_read_on = date(2026, 10, 1)
        db.commit()
        page = _day_page({})
        await _run_range(_scraper(page), db, lawyer)
        asked = [datetime.strptime(p, "%d/%m/%Y").date() for p in page.posted]
        assert all(d.weekday() != 6 and d.weekday() != 5 for d in asked)
        assert date(2026, 10, 12) not in asked  # feriado
        assert asked == [date(2026, 10, d) for d in (2, 5, 6, 7, 8, 9, 13)]

    @pytest.mark.asyncio
    async def test_cooldown_tripping_mid_range_stops_the_range(
        self, monkeypatch, limiter, db, log
    ):
        from app.services.shape_cooldown import get_shape_cooldown, reset_shape_cooldown

        reset_shape_cooldown()
        _enable(monkeypatch)
        lawyer = _lawyer(db)
        lawyer.estado_diario_last_read_on = LAST_READ
        db.commit()

        def trip_after_first(fecha):
            if fecha == "08/10/2026":
                get_shape_cooldown().trip()

        page = _day_page({}, on_post=trip_after_first)
        try:
            await _run_range(_scraper(page), db, lawyer)
        finally:
            reset_shape_cooldown()
        assert page.posted == ["08/10/2026"]  # no further days requested
        assert _watermark(db, lawyer) == date(2026, 10, 8)

    @pytest.mark.asyncio
    async def test_rows_of_every_day_read_are_returned(
        self, monkeypatch, limiter, db, log
    ):
        _enable(monkeypatch)
        lawyer = _lawyer(db)
        lawyer.estado_diario_last_read_on = LAST_READ
        db.commit()
        page = _day_page({
            "08/10/2026": _resp(_table([("C-1-2026", "08/10/2026", "A/B", "1 Civil")])),
            "13/10/2026": _resp(_table([("C-2-2026", "13/10/2026", "A/B", "1 Civil")])),
        })
        rows = await _run_range(_scraper(page), db, lawyer)
        assert sorted(r.rol for r in rows) == ["C-1-2026", "C-2-2026"]

    @pytest.mark.asyncio
    async def test_one_pair_of_log_lines_per_day_with_unchanged_format(
        self, monkeypatch, limiter, db, log
    ):
        _enable(monkeypatch)
        lawyer = _lawyer(db)
        lawyer.estado_diario_last_read_on = LAST_READ
        db.commit()
        page = _day_page({
            "08/10/2026": _resp(_table([("C-1-2026", "08/10/2026", "A/B", "1 Civil")])),
        })
        await _run_range(_scraper(page), db, lawyer)
        lines = [l for l in _logged(log) if l.startswith("ESTADO_DIARIO_PROBE ")]
        assert [re.search(r"date=(\S+)", l).group(1) for l in lines] == EXPECTED_3
        assert f"lawyer={lawyer.id} date=08/10/2026 outcome=ok rows=1 in_db=0 " in lines[0]
        assert "outcome=no_activity rows=0" in lines[1]

    @pytest.mark.asyncio
    async def test_each_request_goes_through_limiter_and_delay(
        self, monkeypatch, limiter, db, log
    ):
        _enable(monkeypatch)
        lawyer = _lawyer(db)
        lawyer.estado_diario_last_read_on = LAST_READ
        db.commit()
        await _run_range(_scraper(_day_page({})), db, lawyer)
        assert limiter.acquired == 3
        assert probe.asyncio.sleep.await_count == 3

    @pytest.mark.asyncio
    async def test_unknown_lawyer_row_does_not_break_the_probe(
        self, monkeypatch, limiter, db, log
    ):
        _enable(monkeypatch)
        page = _day_page({})
        rows = await _run(_scraper(page), db)  # lawyer_id=8 has no row
        assert page.posted == ["13/10/2026"]
        assert rows == []


class TestCaratuladoIsCaptured:
    """The feed row carries the caratulado; keeping it is how we find out WHOSE
    cases the ones missing from our database are (the firm's clients, or
    unrelated third parties). Without it, a missing ROL says nothing."""

    def test_the_parser_keeps_the_caratulado_of_each_row(self):
        r = probe.classify_response(_resp(_table([
            ("C-1-2026", "13/10/2026", "BANCO X / PEREZ", "1 Civil"),
            ("C-2-2026", "13/10/2026", "RETAIL Y / SOTO", "2 Civil"),
        ])))
        assert r.outcome == probe.OK
        assert [row.caratulado for row in r.rows] == [
            "BANCO X / PEREZ",
            "RETAIL Y / SOTO",
        ]

    def test_a_row_without_caratulado_keeps_none_and_does_not_break(self):
        # The header check guarantees the column exists, but a short data row
        # must not raise: the probe never breaks the cycle.
        body = (
            "<table><thead><tr><th>Rol</th><th>Fecha</th><th>Caratulado</th>"
            "<th>Tribunal</th></tr></thead>"
            "<tbody><tr><td>C-9-2026</td><td>13/10/2026</td></tr></tbody></table>"
        )
        r = probe.classify_response(_resp(body))
        assert r.outcome == probe.OK
        assert r.rows[0].rol == "C-9-2026"
        assert r.rows[0].caratulado is None

    def test_the_sample_line_shows_the_caratulado(self, log):
        # Uses the module's mocked logger, not caplog: another test in the suite
        # disables the real loggers (alembic's fileConfig), so a caplog
        # assertion here passes alone and fails in a full run.
        stats = probe.Staleness(
            in_db=0, not_in_db=1, never_checked=0, median_days=None, max_days=None,
            samples={"not_in_db": [
                probe.FeedRow(rol="C-7-2026", tribunal="9 Civil",
                              caratulado="BANCO X / PEREZ"),
            ]},
        )
        probe._log_samples(17, "13/10/2026", stats)
        linea = "\n".join(_logged(log))
        assert "BANCO X / PEREZ" in linea
        assert "C-7-2026" in linea
