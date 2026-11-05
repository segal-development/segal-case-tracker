"""Telegram notifier: plain-text operational alerts to the operator's phone.

The Bot API takes the token in the URL path (``/bot<TOKEN>/sendMessage``), so the
token leaks through anything that prints the URL or the raw exception text of the
request. This module therefore:

* never logs the URL or ``str(exc)``; failures log only the exception class and
  the HTTP status,
* never returns anything but a bool,
* redacts the token from the ``httpx``/``httpcore`` loggers, which print the
  request line at INFO on their own.

An alert that breaks the worker is worse than no alert, so ``send_telegram``
never raises.
"""

import logging
import re

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
_TIMEOUT_SECONDS = 10

#: True once the "channel disabled" warning was logged: one per process, not per call.
_disabled_warned = False

_BOT_PATH = re.compile(r"/bot\d+:[A-Za-z0-9_-]+")


class _RedactBotToken(logging.Filter):
    """Masks the bot token in a record instead of dropping the record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            redacted = _BOT_PATH.sub("/bot***", message)
            token = settings.TELEGRAM_BOT_TOKEN
            if token:
                redacted = redacted.replace(token, "***")
            if redacted != message:
                record.msg = redacted
                record.args = ()
        except Exception:  # noqa: BLE001 — a log filter must never break logging
            pass
        return True


def _install_log_redaction() -> None:
    for name in ("httpx", "httpcore"):
        target = logging.getLogger(name)
        if not any(isinstance(f, _RedactBotToken) for f in target.filters):
            target.addFilter(_RedactBotToken())


_install_log_redaction()


def is_enabled() -> bool:
    """True when both the bot token and the chat id are configured."""
    return bool(settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID)


def send_telegram(text: str) -> bool:
    """Send ``text`` as plain text. True only if Telegram answered 200.

    Never raises and never logs or returns the token.
    """
    global _disabled_warned

    if not is_enabled():
        if not _disabled_warned:
            _disabled_warned = True
            logger.warning(
                "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not configured; "
                "Telegram alerts are disabled"
            )
        return False

    try:
        response = httpx.post(
            _API_URL.format(token=settings.TELEGRAM_BOT_TOKEN),
            json={"chat_id": settings.TELEGRAM_CHAT_ID, "text": text},
            timeout=_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 — see module docstring
        logger.warning("Telegram alert not sent: %s", type(exc).__name__)
        return False

    if response.status_code != 200:
        logger.warning("Telegram alert not sent: HTTP %s", response.status_code)
        return False
    return True
