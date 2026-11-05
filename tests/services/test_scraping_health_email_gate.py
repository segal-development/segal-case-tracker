"""The scraping-down alert honours the outgoing e-mail gate."""
from app.config import settings as cfg
from app.services import email_gate
from app.services import scraping_health as mod
from app.services.scraping_health import ESTADO_FALLANDO, SaludScraping


def _salud():
    return SaludScraping(
        estado=ESTADO_FALLANDO,
        ultimo_exito=None,
        horas_sin_exito=None,
        fallas_consecutivas=5,
        motivo="5 fallaron seguidas",
    )


def _configurar(monkeypatch, kinds, recipients):
    monkeypatch.setattr(cfg, "SMTP_HOST", "smtp.test")
    monkeypatch.setattr(cfg, "SMTP_USER", "")
    monkeypatch.setattr(cfg, "SMTP_USE_TLS", False)
    monkeypatch.setattr(cfg, "SCRAPING_ALERT_EMAIL", "operador@segal.cl")
    monkeypatch.setattr(email_gate.settings, "EMAIL_ALLOWED_KINDS", kinds)
    monkeypatch.setattr(email_gate.settings, "EMAIL_ALLOWED_RECIPIENTS", recipients)


class _SmtpFalso:
    enviados: list = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def send_message(self, msg):
        type(self).enviados.append(msg)


def test_suprimido_no_entrega_nada_a_smtp(monkeypatch):
    _SmtpFalso.enviados = []
    _configurar(monkeypatch, "plazos_fatales_estudio", "carla@segal.cl")
    monkeypatch.setattr(mod.smtplib, "SMTP", _SmtpFalso, raising=False)

    assert mod._enviar_email(_salud()) is False
    assert _SmtpFalso.enviados == []


def test_permitido_igual_se_envia(monkeypatch):
    _SmtpFalso.enviados = []
    _configurar(monkeypatch, "scraping", "operador@segal.cl")
    monkeypatch.setattr(mod.smtplib, "SMTP", _SmtpFalso, raising=False)

    assert mod._enviar_email(_salud()) is True
    assert len(_SmtpFalso.enviados) == 1
