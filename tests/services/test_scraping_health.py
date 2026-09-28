"""Salud del scraping: detectar que se cayó, y avisar una sola vez.

Estos tests existen por dos incidentes reales, no por hipótesis:

* Sysgal quedó sin consultar desde el 3 de septiembre y nadie se enteró en
  22 días.
* El scraping acumuló 29 corridas fallidas el viernes a la noche y después
  estuvo dos días en silencio. Tampoco avisó nadie.

Las dos veces el dato estaba en la base; lo que faltaba era que alguien lo
mirara.
"""

from datetime import datetime, timedelta

import pytest

from app.models.alerta_operativa import AlertaOperativa
from app.models.lawyer import Lawyer
from app.models.sync_history import SyncHistory
from app.services.scraping_health import (
    ESTADO_CAIDO,
    ESTADO_FALLANDO,
    ESTADO_OK,
    TIPO_SCRAPING,
    evaluar_salud_scraping,
    registrar_alerta_enviada,
    alerta_en_cooldown,
)

AHORA = datetime(2026, 9, 28, 12, 0, 0)


@pytest.fixture
def lawyer(db):
    obj = Lawyer(rut="11111111-1", name="Abogado Salud", is_firm_lawyer=True, is_active=True)
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


def _corrida(db, lawyer, *, status: str, hace_horas: float, error: str | None = None):
    inicio = AHORA - timedelta(hours=hace_horas)
    db.add(SyncHistory(
        lawyer_id=lawyer.id,
        competencia="civil",
        status=status,
        error_message=error,
        started_at=inicio,
        completed_at=inicio + timedelta(seconds=30),
    ))
    db.commit()


class TestEstadoSano:
    def test_una_corrida_reciente_completada_es_ok(self, db, lawyer):
        _corrida(db, lawyer, status="completed", hace_horas=0.5)

        salud = evaluar_salud_scraping(db, ahora=AHORA)

        assert salud.estado == ESTADO_OK
        assert salud.fallas_consecutivas == 0
        assert salud.horas_sin_exito is not None and salud.horas_sin_exito < 1

    def test_una_parcial_tambien_cuenta_como_exito(self, db, lawyer):
        """`partial` significa que trajo datos: el scraping está vivo."""
        _corrida(db, lawyer, status="partial", hace_horas=1)

        assert evaluar_salud_scraping(db, ahora=AHORA).estado == ESTADO_OK

    def test_fallas_sueltas_entre_exitos_no_alarman(self, db, lawyer):
        _corrida(db, lawyer, status="failed", hace_horas=3)
        _corrida(db, lawyer, status="completed", hace_horas=2)
        _corrida(db, lawyer, status="failed", hace_horas=1)

        salud = evaluar_salud_scraping(db, ahora=AHORA)

        assert salud.estado == ESTADO_OK
        assert salud.fallas_consecutivas == 1


class TestCadenaDeFallas:
    def test_varias_fallas_seguidas_es_fallando(self, db, lawyer):
        """El viernes fueron 29 seguidas. Con el umbral en 5, habría avisado
        veinticuatro fallas antes."""
        _corrida(db, lawyer, status="completed", hace_horas=6)
        for h in (5, 4, 3, 2, 1):
            _corrida(db, lawyer, status="failed", hace_horas=h, error="Not authenticated")

        salud = evaluar_salud_scraping(db, ahora=AHORA)

        assert salud.estado == ESTADO_FALLANDO
        assert salud.fallas_consecutivas == 5
        assert "Not authenticated" in salud.motivo

    def test_el_umbral_se_puede_ajustar(self, db, lawyer):
        for h in (3, 2, 1):
            _corrida(db, lawyer, status="failed", hace_horas=h)

        assert evaluar_salud_scraping(db, ahora=AHORA, max_fallas=10).estado != ESTADO_FALLANDO
        assert evaluar_salud_scraping(db, ahora=AHORA, max_fallas=3).estado == ESTADO_FALLANDO


class TestSilencio:
    def test_sin_exitos_recientes_es_caido(self, db, lawyer):
        """Los dos días de silencio del fin de semana."""
        _corrida(db, lawyer, status="completed", hace_horas=50)

        salud = evaluar_salud_scraping(db, ahora=AHORA, horas_sin_exito=6)

        assert salud.estado == ESTADO_CAIDO
        assert salud.horas_sin_exito == pytest.approx(50, abs=0.1)
        assert "50" in salud.motivo or "horas" in salud.motivo

    def test_sin_ninguna_corrida_no_inventa_una_caida(self, db):
        """Una base recién creada no es un incidente: no hay nada que comparar."""
        salud = evaluar_salud_scraping(db, ahora=AHORA)

        assert salud.estado == ESTADO_OK
        assert salud.ultimo_exito is None

    def test_la_cadena_de_fallas_gana_sobre_el_silencio(self, db, lawyer):
        """Si además está fallando, el motivo útil es la falla, no el reloj."""
        _corrida(db, lawyer, status="completed", hace_horas=40)
        for h in (5, 4, 3, 2, 1):
            _corrida(db, lawyer, status="failed", hace_horas=h, error="Shape")

        assert evaluar_salud_scraping(db, ahora=AHORA).estado == ESTADO_FALLANDO


class TestCooldown:
    """Una alerta que se repite en cada ciclo se convierte en ruido, y el ruido
    se ignora. Es la misma razón por la que nadie miraba sync_history."""

    def test_recien_enviada_queda_en_cooldown(self, db):
        registrar_alerta_enviada(db, TIPO_SCRAPING, "detalle", ahora=AHORA)

        assert alerta_en_cooldown(db, TIPO_SCRAPING, ahora=AHORA + timedelta(hours=1)) is True

    def test_pasado_el_cooldown_vuelve_a_avisar(self, db):
        registrar_alerta_enviada(db, TIPO_SCRAPING, "detalle", ahora=AHORA)

        assert alerta_en_cooldown(
            db, TIPO_SCRAPING, ahora=AHORA + timedelta(hours=7), cooldown_horas=6
        ) is False

    def test_sin_alertas_previas_no_hay_cooldown(self, db):
        assert alerta_en_cooldown(db, TIPO_SCRAPING, ahora=AHORA) is False

    def test_queda_registro_de_cada_aviso(self, db):
        registrar_alerta_enviada(db, TIPO_SCRAPING, "primera", ahora=AHORA)
        registrar_alerta_enviada(db, TIPO_SCRAPING, "segunda", ahora=AHORA + timedelta(hours=8))

        filas = db.query(AlertaOperativa).order_by(AlertaOperativa.id).all()
        assert [f.detalle for f in filas] == ["primera", "segunda"]


class TestEnvioDelAviso:
    """El envío tiene tres formas de hacer daño: no avisar, avisar de más, o
    romper el worker. Las tres se fijan acá."""

    @pytest.fixture(autouse=True)
    def _smtp_configurado(self, monkeypatch):
        from app.config import settings as cfg

        monkeypatch.setattr(cfg, "SMTP_HOST", "smtp.test")
        monkeypatch.setattr(cfg, "SMTP_PORT", 587)
        monkeypatch.setattr(cfg, "SMTP_USER", "")
        monkeypatch.setattr(cfg, "SMTP_USE_TLS", False)
        monkeypatch.setattr(cfg, "SCRAPING_ALERT_EMAIL", "operador@segal.cl")
        monkeypatch.setattr(cfg, "SMTP_FROM", "sistema@segal.cl")

    def _fallando(self, db, lawyer):
        _corrida(db, lawyer, status="completed", hace_horas=9)
        for h in (5, 4, 3, 2, 1):
            _corrida(db, lawyer, status="failed", hace_horas=h, error="Not authenticated")

    def test_avisa_cuando_esta_fallando(self, db, lawyer, monkeypatch):
        from app.services import scraping_health as mod

        enviados = []
        monkeypatch.setattr(mod.smtplib, "SMTP", _smtp_falso(enviados), raising=False)
        self._fallando(db, lawyer)

        salud = mod.revisar_y_avisar(db, ahora=AHORA)

        assert salud.estado == ESTADO_FALLANDO
        assert len(enviados) == 1
        assert "operador@segal.cl" == enviados[0]["To"]
        assert "fallaron seguidas" in enviados[0].get_content()

    def test_no_avisa_dos_veces_seguidas(self, db, lawyer, monkeypatch):
        from app.services import scraping_health as mod

        enviados = []
        monkeypatch.setattr(mod.smtplib, "SMTP", _smtp_falso(enviados), raising=False)
        self._fallando(db, lawyer)

        mod.revisar_y_avisar(db, ahora=AHORA)
        mod.revisar_y_avisar(db, ahora=AHORA)

        assert len(enviados) == 1, "el segundo ciclo del worker volvió a avisar"

    def test_sin_casilla_configurada_no_avisa_y_no_rompe(self, db, lawyer, monkeypatch):
        from app.config import settings as cfg
        from app.services import scraping_health as mod

        monkeypatch.setattr(cfg, "SCRAPING_ALERT_EMAIL", "")
        self._fallando(db, lawyer)

        salud = mod.revisar_y_avisar(db, ahora=AHORA)  # no debe levantar

        assert salud.estado == ESTADO_FALLANDO
        assert db.query(AlertaOperativa).count() == 0

    def test_un_smtp_caido_no_rompe_el_worker(self, db, lawyer, monkeypatch):
        from app.services import scraping_health as mod

        def _explota(*a, **k):
            raise OSError("smtp caido")

        monkeypatch.setattr(mod.smtplib, "SMTP", _explota, raising=False)
        self._fallando(db, lawyer)

        salud = mod.revisar_y_avisar(db, ahora=AHORA)  # no debe levantar

        assert salud.estado == ESTADO_FALLANDO
        # No se registra el aviso: no se envió, así que se puede reintentar.
        assert db.query(AlertaOperativa).count() == 0

    def test_todo_sano_no_manda_nada(self, db, lawyer, monkeypatch):
        from app.services import scraping_health as mod

        enviados = []
        monkeypatch.setattr(mod.smtplib, "SMTP", _smtp_falso(enviados), raising=False)
        _corrida(db, lawyer, status="completed", hace_horas=0.5)

        mod.revisar_y_avisar(db, ahora=AHORA)

        assert enviados == []


def _smtp_falso(enviados: list):
    """Un SMTP de mentira que guarda los mensajes en vez de enviarlos."""

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
            enviados.append(msg)

    return _FakeSMTP


def _corrida_real(db, lawyer, *, status: str, hace_horas: float):
    """Como `_corrida`, pero anclada al reloj REAL.

    El endpoint evalua con `datetime.utcnow()` y no hay por donde inyectarle
    otro reloj: la peticion entra por HTTP. Anclar estas filas a un `AHORA`
    fijo hacia que los tests pasaran o fallaran segun la hora del dia a la que
    corriera la suite — y asi fue: pasaban al mediodia y fallaban a la tarde,
    cuando la distancia superaba el umbral de 6 horas.
    """
    inicio = datetime.utcnow() - timedelta(hours=hace_horas)
    db.add(SyncHistory(
        lawyer_id=lawyer.id,
        competencia="civil",
        status=status,
        started_at=inicio,
        completed_at=inicio + timedelta(seconds=30),
    ))
    db.commit()


class TestEndpointExterno:
    """`GET /health/scraping` es lo unico que puede detectar que el worker
    murio: un proceso muerto no se avisa a si mismo."""

    def test_sano_responde_200(self, client, db, lawyer):
        _corrida_real(db, lawyer, status="completed", hace_horas=0.5)

        resp = client.get("/health/scraping")

        assert resp.status_code == 200
        assert resp.json() == {"scraping": ESTADO_OK}

    def test_fallando_responde_503(self, client, db, lawyer):
        _corrida_real(db, lawyer, status="completed", hace_horas=9)
        for h in (5, 4, 3, 2, 1):
            _corrida_real(db, lawyer, status="failed", hace_horas=h)

        resp = client.get("/health/scraping")

        assert resp.status_code == 503

    def test_no_filtra_detalle_operativo(self, client, db, lawyer):
        """Va sin autenticacion, asi que no puede contar como esta armado
        el sistema por dentro."""
        _corrida_real(db, lawyer, status="completed", hace_horas=0.5)

        cuerpo = client.get("/health/scraping").text

        assert "sync_history" not in cuerpo
        assert "lawyer" not in cuerpo.lower()
