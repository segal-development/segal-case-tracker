"""Scraping alerts through every configured channel (email and Telegram).

Telegram must work with SMTP/email NOT configured; email behaves as before when
it is configured; the cooldown and the bookkeeping are shared. The HTTP layer is
mocked in every test: no real network, ever.
"""
import logging
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import httpx
import pytest

from app.models.alerta_operativa import AlertaOperativa
from app.services import scraping_health as mod
from app.services import telegram_notifier
from app.services.scraping_health import (
    ESTADO_CAIDO,
    ESTADO_FALLANDO,
    ESTADO_OK,
    TIPO_SCRAPING,
    SaludScraping,
    enviar_alerta_scraping,
)

AHORA = datetime(2026, 10, 28, 12, 0, 0)
FAKE_TOKEN = "123456:FAKE-TOKEN-NOT-REAL"
FAKE_CHAT = "-100987654321"


class FakeResponse:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.text = ""


def _salud(estado: str = ESTADO_FALLANDO, **over) -> SaludScraping:
    base = dict(
        estado=estado,
        ultimo_exito=AHORA - timedelta(hours=9),
        horas_sin_exito=9.0,
        fallas_consecutivas=5,
        motivo="5 corridas de sync fallaron seguidas. Último error: Not authenticated",
    )
    base.update(over)
    return SaludScraping(**base)


@pytest.fixture
def post(monkeypatch) -> MagicMock:
    fake = MagicMock(return_value=FakeResponse(200))
    monkeypatch.setattr(telegram_notifier.httpx, "post", fake)
    return fake


@pytest.fixture(autouse=True)
def _channels_off(monkeypatch, post):
    """Start from 'nothing configured'; each test turns on what it needs."""
    for name, value in {
        "SMTP_HOST": "",
        "SCRAPING_ALERT_EMAIL": "",
        "SMTP_PORT": 587,
        "SMTP_USER": "",
        "SMTP_USE_TLS": False,
        "SMTP_FROM": "sistema@segal.cl",
        "TELEGRAM_BOT_TOKEN": "",
        "TELEGRAM_CHAT_ID": "",
    }.items():
        monkeypatch.setattr(mod.settings, name, value)
    monkeypatch.setattr(telegram_notifier, "_disabled_warned", False, raising=False)


@pytest.fixture
def telegram_on(monkeypatch):
    monkeypatch.setattr(mod.settings, "TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setattr(mod.settings, "TELEGRAM_CHAT_ID", FAKE_CHAT)


@pytest.fixture
def email_on(monkeypatch):
    sent: list = []

    class _FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, **k):
            pass

        def login(self, *a):
            pass

        def send_message(self, msg):
            sent.append(msg)

    monkeypatch.setattr(mod.settings, "SMTP_HOST", "smtp.test")
    monkeypatch.setattr(mod.settings, "SCRAPING_ALERT_EMAIL", "operador@segal.cl")
    monkeypatch.setattr(mod.smtplib, "SMTP", _FakeSMTP, raising=False)
    return sent


def _rows(db, tipo=TIPO_SCRAPING):
    return db.query(AlertaOperativa).filter(AlertaOperativa.tipo == tipo).all()


class TestChannelSelection:
    def test_telegram_alone_delivers_when_email_is_not_configured(
        self, db, post, telegram_on
    ):
        assert enviar_alerta_scraping(db, _salud(), ahora=AHORA) is True

        post.assert_called_once()
        assert len(_rows(db)) == 1

    def test_email_alone_delivers_when_telegram_is_not_configured(
        self, db, post, email_on
    ):
        assert enviar_alerta_scraping(db, _salud(), ahora=AHORA) is True

        assert len(email_on) == 1
        assert email_on[0]["To"] == "operador@segal.cl"
        post.assert_not_called()
        assert len(_rows(db)) == 1

    def test_both_channels_deliver_when_both_are_configured(
        self, db, post, telegram_on, email_on
    ):
        assert enviar_alerta_scraping(db, _salud(), ahora=AHORA) is True

        assert len(email_on) == 1
        post.assert_called_once()
        assert len(_rows(db)) == 1, "bookkeeping must be recorded once, not per channel"

    def test_nothing_configured_returns_false_and_records_nothing(self, db, post):
        assert enviar_alerta_scraping(db, _salud(), ahora=AHORA) is False

        post.assert_not_called()
        assert db.query(AlertaOperativa).count() == 0

    def test_only_smtp_host_without_recipient_is_not_an_email_channel(
        self, db, post, monkeypatch, telegram_on
    ):
        monkeypatch.setattr(mod.settings, "SMTP_HOST", "smtp.test")

        assert enviar_alerta_scraping(db, _salud(), ahora=AHORA) is True

        post.assert_called_once()

    def test_healthy_state_sends_nothing(self, db, post, telegram_on, email_on):
        assert enviar_alerta_scraping(db, _salud(ESTADO_OK), ahora=AHORA) is False

        post.assert_not_called()
        assert email_on == []
        assert db.query(AlertaOperativa).count() == 0


class TestPartialFailures:
    def test_telegram_http_error_with_no_email_is_not_delivered(
        self, db, post, telegram_on
    ):
        post.return_value = FakeResponse(500)

        assert enviar_alerta_scraping(db, _salud(), ahora=AHORA) is False

        assert db.query(AlertaOperativa).count() == 0, "not sent: it can be retried"

    def test_telegram_down_but_email_ok_counts_as_delivered(
        self, db, post, telegram_on, email_on
    ):
        post.side_effect = httpx.ConnectError("down")

        assert enviar_alerta_scraping(db, _salud(), ahora=AHORA) is True

        assert len(email_on) == 1
        assert len(_rows(db)) == 1

    def test_email_down_but_telegram_ok_counts_as_delivered(
        self, db, post, telegram_on, email_on, monkeypatch
    ):
        def _explota(*a, **k):
            raise OSError("smtp down")

        monkeypatch.setattr(mod.smtplib, "SMTP", _explota, raising=False)

        assert enviar_alerta_scraping(db, _salud(), ahora=AHORA) is True

        post.assert_called_once()
        assert len(_rows(db)) == 1

    def test_both_down_returns_false_without_raising(
        self, db, post, telegram_on, email_on, monkeypatch
    ):
        post.side_effect = httpx.ReadTimeout("slow")
        monkeypatch.setattr(
            mod.smtplib, "SMTP", MagicMock(side_effect=OSError("down")), raising=False
        )

        assert enviar_alerta_scraping(db, _salud(), ahora=AHORA) is False

        assert db.query(AlertaOperativa).count() == 0


class TestCooldown:
    def test_second_call_inside_the_cooldown_sends_nothing(self, db, post, telegram_on):
        assert enviar_alerta_scraping(db, _salud(), ahora=AHORA) is True
        assert enviar_alerta_scraping(db, _salud(), ahora=AHORA) is False

        post.assert_called_once()
        assert len(_rows(db)) == 1

    def test_after_the_cooldown_it_notifies_again(self, db, post, telegram_on):
        enviar_alerta_scraping(db, _salud(), ahora=AHORA)

        later = AHORA + timedelta(hours=mod.settings.SCRAPING_ALERT_COOLDOWN_HOURS + 1)
        assert enviar_alerta_scraping(db, _salud(), ahora=later) is True

        assert post.call_count == 2
        assert len(_rows(db)) == 2

    def test_a_failed_telegram_does_not_start_the_cooldown(self, db, post, telegram_on):
        post.return_value = FakeResponse(502)
        assert enviar_alerta_scraping(db, _salud(), ahora=AHORA) is False

        post.return_value = FakeResponse(200)
        assert enviar_alerta_scraping(db, _salud(), ahora=AHORA) is True


class TestTelegramText:
    def _sent_text(self, post) -> str:
        return post.call_args.kwargs["json"]["text"]

    def test_message_carries_state_reason_hours_and_time(self, db, post, telegram_on):
        enviar_alerta_scraping(db, _salud(), ahora=AHORA)

        text = self._sent_text(post)
        assert "fallando" in text
        assert "Not authenticated" in text
        assert "9" in text
        assert "28-10-2026 12:00" in text

    def test_message_for_caido_names_the_state(self, db, post, telegram_on):
        salud = _salud(ESTADO_CAIDO, motivo="No hay una corrida de sync exitosa desde hace 9 horas")
        enviar_alerta_scraping(db, salud, ahora=AHORA)

        assert "caido" in self._sent_text(post)

    def test_message_without_any_success_says_so(self, db, post, telegram_on):
        salud = _salud(ultimo_exito=None, horas_sin_exito=None)
        enviar_alerta_scraping(db, salud, ahora=AHORA)

        assert "nunca" in self._sent_text(post).lower()

    def test_message_has_no_credentials_and_is_plain_and_short(self, db, post, telegram_on):
        enviar_alerta_scraping(db, _salud(), ahora=AHORA)

        text = self._sent_text(post)
        assert FAKE_TOKEN not in text
        assert len(text) < 1000
        assert "parse_mode" not in post.call_args.kwargs["json"]

    def test_builder_is_pure_and_deterministic(self):
        assert mod.texto_telegram(_salud(), ahora=AHORA) == mod.texto_telegram(
            _salud(), ahora=AHORA
        )


class TestTokenNeverLeaks:
    def test_logs_and_return_value_are_clean_when_telegram_blows_up(
        self, db, post, telegram_on, caplog
    ):
        post.side_effect = httpx.ConnectError(
            f"failed https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage"
        )

        with caplog.at_level(logging.DEBUG):
            result = enviar_alerta_scraping(db, _salud(), ahora=AHORA)

        assert result is False
        assert FAKE_TOKEN not in caplog.text
        assert all(FAKE_TOKEN not in str(r.__dict__) for r in caplog.records)

    def test_logs_are_clean_on_success(self, db, post, telegram_on, caplog):
        with caplog.at_level(logging.DEBUG):
            enviar_alerta_scraping(db, _salud(), ahora=AHORA)

        assert FAKE_TOKEN not in caplog.text

    def test_bookkeeping_rows_do_not_contain_the_token(self, db, post, telegram_on):
        enviar_alerta_scraping(db, _salud(), ahora=AHORA)

        row = _rows(db)[0]
        assert FAKE_TOKEN not in f"{row.tipo}{row.estado}{row.detalle}"


# ─────────────────────────────────────────────────────────────────────────────
# Breaker event (S4): one persisted event, de-duplicated by its own cooldown
# ─────────────────────────────────────────────────────────────────────────────


class TestAvisarBloqueoIp:
    def _avisar(self, db, **over):
        kwargs = dict(rechazos=3, minutos=90, ahora=AHORA)
        kwargs.update(over)
        return mod.avisar_bloqueo_ip(db, **kwargs)

    def test_sends_the_exact_spanish_message_once(self, db, post, telegram_on):
        assert self._avisar(db) is True

        post.assert_called_once()
        assert post.call_args.kwargs["json"]["text"] == (
            "El PJUD rechazó 3 sesiones nuevas seguidas; la IP de la estación "
            "probablemente está bloqueada. El worker descansa 90 minutos."
        )

    def test_persists_one_event_of_its_own_type(self, db, post, telegram_on):
        self._avisar(db)

        rows = _rows(db, mod.TIPO_BLOQUEO_IP)
        assert mod.TIPO_BLOQUEO_IP == "scraping_bloqueo_ip"
        assert len(rows) == 1
        assert rows[0].enviada_at == AHORA
        assert "3 sesiones" in rows[0].detalle

    def test_second_trip_inside_the_window_sends_and_records_nothing(
        self, db, post, telegram_on
    ):
        self._avisar(db)
        assert self._avisar(db, ahora=AHORA + timedelta(minutes=95)) is False

        post.assert_called_once()
        assert len(_rows(db, mod.TIPO_BLOQUEO_IP)) == 1

    def test_a_trip_after_the_window_is_a_new_incident(self, db, post, telegram_on):
        self._avisar(db)
        later = AHORA + timedelta(hours=mod.settings.SCRAPING_ALERT_COOLDOWN_HOURS + 1)

        assert self._avisar(db, ahora=later) is True

        assert post.call_count == 2
        assert len(_rows(db, mod.TIPO_BLOQUEO_IP)) == 2

    def test_event_is_persisted_even_if_telegram_is_not_configured(self, db, post):
        assert self._avisar(db) is False

        post.assert_not_called()
        assert len(_rows(db, mod.TIPO_BLOQUEO_IP)) == 1, "the admin banner reads it"

    def test_event_is_persisted_even_if_telegram_fails_and_nothing_raises(
        self, db, post, telegram_on
    ):
        post.side_effect = httpx.ConnectError("down")

        assert self._avisar(db) is False

        assert len(_rows(db, mod.TIPO_BLOQUEO_IP)) == 1

    def test_dedupe_is_independent_from_the_health_alert(self, db, post, telegram_on):
        enviar_alerta_scraping(db, _salud(), ahora=AHORA)

        assert self._avisar(db) is True

        assert post.call_count == 2

    def test_token_never_appears_in_logs(self, db, post, telegram_on, caplog):
        post.side_effect = RuntimeError(f"token={FAKE_TOKEN}")

        with caplog.at_level(logging.DEBUG):
            self._avisar(db)

        assert FAKE_TOKEN not in caplog.text


class TestUltimoBloqueoIp:
    def _evento(self, db, *, hace_horas: float, detalle: str = "x", tipo=None):
        db.add(
            AlertaOperativa(
                tipo=tipo or mod.TIPO_BLOQUEO_IP,
                estado=None,
                detalle=detalle,
                enviada_at=AHORA - timedelta(hours=hace_horas),
            )
        )
        db.commit()

    def test_none_without_events(self, db):
        assert mod.ultimo_bloqueo_ip(db, ahora=AHORA) is None

    def test_returns_an_event_inside_the_last_12_hours(self, db):
        self._evento(db, hace_horas=11.9, detalle="dentro")

        found = mod.ultimo_bloqueo_ip(db, ahora=AHORA)

        assert found is not None and found.detalle == "dentro"

    def test_ignores_an_event_older_than_12_hours(self, db):
        self._evento(db, hace_horas=12.1)

        assert mod.ultimo_bloqueo_ip(db, ahora=AHORA) is None

    def test_returns_the_most_recent_one(self, db):
        self._evento(db, hace_horas=8, detalle="viejo")
        self._evento(db, hace_horas=2, detalle="nuevo")

        assert mod.ultimo_bloqueo_ip(db, ahora=AHORA).detalle == "nuevo"

    def test_other_alert_types_do_not_count(self, db):
        self._evento(db, hace_horas=1, tipo=TIPO_SCRAPING)

        assert mod.ultimo_bloqueo_ip(db, ahora=AHORA) is None
