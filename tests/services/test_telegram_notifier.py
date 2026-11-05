"""Telegram notifier: plain-text delivery that can never break its caller.

The bot token travels inside the request URL, so the dangerous failure modes are
leaking it (logs, exception text, return values) and raising into the worker.
The HTTP layer is always mocked: no test here may touch the network.
"""
import logging
from unittest.mock import MagicMock

import httpx
import pytest

from app.services import telegram_notifier as mod

FAKE_TOKEN = "123456:FAKE-TOKEN-NOT-REAL"
FAKE_CHAT = "-100987654321"


class FakeResponse:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.text = f"body mentions {FAKE_TOKEN}"

    def raise_for_status(self) -> None:  # pragma: no cover - must not be relied upon
        raise httpx.HTTPStatusError(
            f"boom https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage",
            request=MagicMock(),
            response=MagicMock(),
        )


@pytest.fixture
def post(monkeypatch) -> MagicMock:
    """Replace the HTTP layer; any un-mocked network call would be a test bug."""
    fake = MagicMock(return_value=FakeResponse(200))
    monkeypatch.setattr(mod.httpx, "post", fake)
    return fake


@pytest.fixture(autouse=True)
def _clean_logging_state():
    """Other tests leave global logging state behind (``logging.disable``, a disabled
    or non-propagating logger), which silently empties ``caplog`` and makes these
    token-leak checks fail only in a full run. Start from a known state and restore."""
    names = (
        "app.services.telegram_notifier",
        "httpx",
        "httpcore",
    )
    loggers = [logging.getLogger(n) for n in names]
    saved_disable = logging.root.manager.disable
    saved = [(lg.disabled, lg.propagate, lg.level) for lg in loggers]
    logging.disable(logging.NOTSET)
    for lg in loggers:
        lg.disabled, lg.propagate, lg.level = False, True, logging.NOTSET
    yield
    logging.disable(saved_disable)
    for lg, (disabled, propagate, level) in zip(loggers, saved):
        lg.disabled, lg.propagate, lg.level = disabled, propagate, level


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(mod.settings, "TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setattr(mod.settings, "TELEGRAM_CHAT_ID", FAKE_CHAT)
    monkeypatch.setattr(mod, "_disabled_warned", False, raising=False)


def _all_log_text(caplog) -> str:
    parts = []
    for r in caplog.records:
        parts.append(r.getMessage())
        parts.append(str(r.__dict__))
        if r.exc_info:
            parts.append(repr(r.exc_info[1]))
    parts.append(caplog.text)
    return "\n".join(parts)


class TestDelivery:
    def test_posts_to_send_message_with_chat_id_and_text(self, post):
        assert mod.send_telegram("hola mundo") is True

        post.assert_called_once()
        args, kwargs = post.call_args
        url = args[0] if args else kwargs["url"]
        assert url == f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage"
        payload = kwargs.get("json")
        assert payload == {"chat_id": FAKE_CHAT, "text": "hola mundo"}
        assert kwargs["timeout"] == 10

    def test_sends_plain_text_without_parse_mode(self, post):
        mod.send_telegram("a_b*c [x](y) <b>")

        payload = post.call_args.kwargs["json"]
        assert "parse_mode" not in payload
        assert payload["text"] == "a_b*c [x](y) <b>"

    def test_non_200_answer_is_a_failure(self, post):
        post.return_value = FakeResponse(401)

        assert mod.send_telegram("x") is False

    def test_return_value_is_a_plain_bool(self, post):
        assert type(mod.send_telegram("x")) is bool


class TestDisabled:
    @pytest.mark.parametrize(
        "token,chat", [("", FAKE_CHAT), (FAKE_TOKEN, ""), ("", "")]
    )
    def test_missing_token_or_chat_disables_the_channel(
        self, post, monkeypatch, token, chat
    ):
        monkeypatch.setattr(mod.settings, "TELEGRAM_BOT_TOKEN", token)
        monkeypatch.setattr(mod.settings, "TELEGRAM_CHAT_ID", chat)

        assert mod.send_telegram("x") is False
        post.assert_not_called()

    def test_warns_once_per_process_not_once_per_call(self, post, monkeypatch, caplog):
        monkeypatch.setattr(mod.settings, "TELEGRAM_BOT_TOKEN", "")

        with caplog.at_level(logging.DEBUG):
            for _ in range(5):
                mod.send_telegram("x")

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "telegram" in warnings[0].getMessage().lower()

    def test_is_enabled_reflects_the_settings(self, monkeypatch):
        assert mod.is_enabled() is True
        monkeypatch.setattr(mod.settings, "TELEGRAM_CHAT_ID", "")
        assert mod.is_enabled() is False


class TestNeverRaises:
    @pytest.mark.parametrize(
        "exc",
        [
            httpx.ConnectError("no route"),
            httpx.ReadTimeout("slow"),
            httpx.ConnectTimeout("slow"),
            RuntimeError("anything"),
        ],
    )
    def test_network_errors_return_false(self, post, exc):
        post.side_effect = exc

        assert mod.send_telegram("x") is False


class TestTokenNeverLeaks:
    @pytest.mark.parametrize(
        "exc",
        [
            httpx.ConnectError(f"failed https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage"),
            httpx.ReadTimeout(f"timeout on /bot{FAKE_TOKEN}/sendMessage"),
            RuntimeError(f"token={FAKE_TOKEN}"),
        ],
    )
    def test_exception_path_logs_only_the_class(self, post, caplog, exc):
        post.side_effect = exc

        with caplog.at_level(logging.DEBUG):
            result = mod.send_telegram("x")

        text = _all_log_text(caplog)
        assert FAKE_TOKEN not in text
        assert "bot123456" not in text
        assert type(exc).__name__ in text
        assert FAKE_TOKEN not in repr(result)
        assert all(r.exc_info is None for r in caplog.records)

    def test_non_200_path_logs_status_but_not_token_or_body(self, post, caplog):
        post.return_value = FakeResponse(403)

        with caplog.at_level(logging.DEBUG):
            result = mod.send_telegram("x")

        text = _all_log_text(caplog)
        assert FAKE_TOKEN not in text
        assert "403" in text
        assert result is False

    def test_success_path_logs_nothing_with_the_token(self, post, caplog):
        with caplog.at_level(logging.DEBUG):
            mod.send_telegram("x")

        assert FAKE_TOKEN not in _all_log_text(caplog)

    def test_disabled_path_logs_nothing_with_the_token(self, post, monkeypatch, caplog):
        monkeypatch.setattr(mod.settings, "TELEGRAM_CHAT_ID", "")

        with caplog.at_level(logging.DEBUG):
            mod.send_telegram("x")

        assert FAKE_TOKEN not in _all_log_text(caplog)


class TestHttpLibraryLogging:
    """httpx logs 'HTTP Request: POST <url>' at INFO, and the URL carries the token."""

    @pytest.mark.parametrize("name", ["httpx", "httpcore"])
    def test_library_request_logs_are_redacted(self, caplog, name):
        url = f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage"

        with caplog.at_level(logging.DEBUG):
            logging.getLogger(name).info('HTTP Request: %s %s "%s"', "POST", url, "HTTP/1.1 200 OK")

        assert FAKE_TOKEN not in _all_log_text(caplog)
        assert any("HTTP Request" in r.getMessage() for r in caplog.records), (
            "redacted, not dropped: the request line stays useful"
        )

    def test_unrelated_http_logs_are_untouched(self, caplog):
        with caplog.at_level(logging.DEBUG):
            logging.getLogger("httpx").info('HTTP Request: GET https://example.test/x "200"')

        assert "https://example.test/x" in caplog.text


class TestSettings:
    def test_defaults_are_empty_so_the_channel_is_off(self):
        from app.config import Settings

        s = Settings(_env_file=None)
        assert s.TELEGRAM_BOT_TOKEN == ""
        assert s.TELEGRAM_CHAT_ID == ""

    def test_read_from_environment(self, monkeypatch):
        from app.config import Settings

        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
        monkeypatch.setenv("TELEGRAM_CHAT_ID", FAKE_CHAT)
        s = Settings(_env_file=None)
        assert s.TELEGRAM_BOT_TOKEN == FAKE_TOKEN
        assert s.TELEGRAM_CHAT_ID == FAKE_CHAT
