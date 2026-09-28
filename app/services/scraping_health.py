"""Salud del scraping: mirar ``sync_history`` y decir si el sistema está vivo.

Existe por dos incidentes, no por prevención abstracta. La integración con
Sysgal estuvo 22 días sin consultar y nadie se enteró; el scraping acumuló 29
corridas fallidas un viernes a la noche y después estuvo dos días en silencio.
Las dos veces el dato estaba en la base: lo que faltaba era que algo lo mirara.

**Límite que conviene tener presente:** la evaluación que corre dentro del
worker detecta que el scraping *falla*, no que el proceso *murió* — un proceso
muerto no se avisa a sí mismo. Para eso está ``GET /health/scraping``, que
expone el mismo estado para que algo externo lo consulte.
"""

import logging
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Optional

from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.config import settings
from app.models.alerta_operativa import AlertaOperativa
from app.models.sync_history import SyncHistory

logger = logging.getLogger(__name__)

#: Identificador del aviso, para el cooldown y la bitácora.
TIPO_SCRAPING = "scraping_detenido"

ESTADO_OK = "ok"
ESTADO_FALLANDO = "fallando"
ESTADO_CAIDO = "caido"

#: Una corrida ``partial`` trajo datos: el scraping está vivo aunque algo haya
#: quedado incompleto. Solo ``failed`` cuenta como falla.
ESTADOS_EXITOSOS = ("completed", "partial")

#: Cuántas corridas se miran hacia atrás para contar la cadena de fallas.
_VENTANA = 50


@dataclass
class SaludScraping:
    """Estado del scraping según lo que quedó registrado en ``sync_history``."""

    estado: str
    ultimo_exito: Optional[datetime]
    horas_sin_exito: Optional[float]
    fallas_consecutivas: int
    motivo: str

    @property
    def requiere_aviso(self) -> bool:
        return self.estado != ESTADO_OK


def evaluar_salud_scraping(
    db: Session,
    *,
    ahora: Optional[datetime] = None,
    horas_sin_exito: Optional[int] = None,
    max_fallas: Optional[int] = None,
) -> SaludScraping:
    """Estado actual del scraping.

    Dos señales, en este orden de prioridad:

    1. **Cadena de fallas** — N corridas fallidas seguidas sin un éxito en el
       medio. Es la señal accionable: el proceso está vivo y el error concreto
       viaja en el aviso.
    2. **Silencio** — ninguna corrida exitosa en las últimas N horas. Cubre el
       caso en que dejó de intentar.

    La cadena gana sobre el silencio: si además está fallando, el motivo útil
    es el error, no el reloj.

    Una base sin ninguna corrida NO es una caída: no hay con qué comparar.
    """
    ahora = ahora or datetime.utcnow()
    limite_horas = horas_sin_exito if horas_sin_exito is not None else settings.SCRAPING_STALE_HOURS
    limite_fallas = max_fallas if max_fallas is not None else settings.SCRAPING_MAX_FALLAS

    recientes = (
        db.query(SyncHistory)
        .order_by(desc(SyncHistory.started_at), desc(SyncHistory.id))
        .limit(_VENTANA)
        .all()
    )
    if not recientes:
        return SaludScraping(
            estado=ESTADO_OK,
            ultimo_exito=None,
            horas_sin_exito=None,
            fallas_consecutivas=0,
            motivo="Todavía no hay corridas registradas.",
        )

    fallas = 0
    ultimo_error = ""
    for corrida in recientes:
        if corrida.status in ESTADOS_EXITOSOS:
            break
        fallas += 1
        if not ultimo_error and corrida.error_message:
            ultimo_error = corrida.error_message

    ultimo_exito = next(
        (c.started_at for c in recientes if c.status in ESTADOS_EXITOSOS), None
    )
    horas = (ahora - ultimo_exito).total_seconds() / 3600 if ultimo_exito else None

    if fallas >= limite_fallas:
        detalle = f" Último error: {ultimo_error}" if ultimo_error else ""
        return SaludScraping(
            estado=ESTADO_FALLANDO,
            ultimo_exito=ultimo_exito,
            horas_sin_exito=horas,
            fallas_consecutivas=fallas,
            motivo=f"{fallas} corridas de sync fallaron seguidas.{detalle}",
        )

    if horas is not None and horas >= limite_horas:
        return SaludScraping(
            estado=ESTADO_CAIDO,
            ultimo_exito=ultimo_exito,
            horas_sin_exito=horas,
            fallas_consecutivas=fallas,
            motivo=(
                f"No hay una corrida de sync exitosa desde hace {horas:.0f} horas "
                f"(la última fue el {ultimo_exito:%d-%m-%Y %H:%M} UTC)."
            ),
        )

    return SaludScraping(
        estado=ESTADO_OK,
        ultimo_exito=ultimo_exito,
        horas_sin_exito=horas,
        fallas_consecutivas=fallas,
        motivo="El scraping viene completando corridas.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Cooldown
# ─────────────────────────────────────────────────────────────────────────────


def alerta_en_cooldown(
    db: Session,
    tipo: str,
    *,
    ahora: Optional[datetime] = None,
    cooldown_horas: Optional[int] = None,
) -> bool:
    """True si ya se avisó de esto hace poco.

    Sin esto, el aviso se repetiría en cada ciclo del worker y se volvería
    ruido — que es cómo mueren las alertas.
    """
    ahora = ahora or datetime.utcnow()
    horas = cooldown_horas if cooldown_horas is not None else settings.SCRAPING_ALERT_COOLDOWN_HOURS

    ultima = (
        db.query(AlertaOperativa.enviada_at)
        .filter(AlertaOperativa.tipo == tipo)
        .order_by(desc(AlertaOperativa.enviada_at))
        .first()
    )
    if ultima is None:
        return False
    return (ahora - ultima[0]) < timedelta(hours=horas)


def registrar_alerta_enviada(
    db: Session,
    tipo: str,
    detalle: str,
    *,
    estado: Optional[str] = None,
    ahora: Optional[datetime] = None,
) -> None:
    """Deja constancia de un aviso enviado. Una fila por aviso, no por tipo."""
    db.add(
        AlertaOperativa(
            tipo=tipo,
            estado=estado,
            detalle=detalle[:500],
            enviada_at=ahora or datetime.utcnow(),
        )
    )
    db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Aviso por correo
# ─────────────────────────────────────────────────────────────────────────────


def _cuerpo(salud: SaludScraping) -> tuple[str, str]:
    """(asunto, texto) del aviso. Dice qué pasó y qué hacer, sin adornos."""
    titulo = {
        ESTADO_FALLANDO: "El scraping está fallando",
        ESTADO_CAIDO: "El scraping dejó de traer datos",
    }.get(salud.estado, "Revisar el scraping")

    ultimo = (
        f"{salud.ultimo_exito:%d-%m-%Y %H:%M} UTC" if salud.ultimo_exito else "nunca"
    )
    texto = (
        f"{salud.motivo}\n\n"
        f"Última corrida exitosa: {ultimo}\n"
        f"Corridas fallidas seguidas: {salud.fallas_consecutivas}\n\n"
        "Qué revisar, en este orden:\n"
        "  1. Que la estación de scraping esté corriendo.\n"
        "  2. Que la sesión de PJUD siga válida (el error suele decirlo).\n"
        "  3. El detalle de las últimas corridas en la tabla sync_history.\n\n"
        "Este aviso no se repite hasta pasadas "
        f"{settings.SCRAPING_ALERT_COOLDOWN_HOURS} horas, aunque el problema siga.\n"
    )
    return f"[Case Tracker] {titulo}", texto


def enviar_alerta_scraping(db: Session, salud: SaludScraping) -> bool:
    """Avisa por correo si corresponde. Devuelve True solo si se envió.

    Nunca levanta: una alerta que rompe el worker es peor que no tener alerta.
    """
    if not salud.requiere_aviso:
        return False

    if not settings.SMTP_HOST or not settings.SCRAPING_ALERT_EMAIL:
        logger.warning(
            "SMTP_HOST/SCRAPING_ALERT_EMAIL sin configurar; no se avisa del "
            "scraping (%s)",
            salud.estado,
        )
        return False

    if alerta_en_cooldown(db, TIPO_SCRAPING):
        return False

    asunto, texto = _cuerpo(salud)
    msg = EmailMessage()
    msg["Subject"] = asunto
    msg["From"] = settings.SMTP_FROM or settings.FROM_EMAIL
    msg["To"] = settings.SCRAPING_ALERT_EMAIL
    msg.set_content(texto)

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as server:
            if settings.SMTP_USE_TLS:
                server.starttls(context=context)
            if settings.SMTP_USER:
                server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.send_message(msg)
    except Exception:
        logger.exception("No se pudo enviar la alerta de scraping")
        return False

    registrar_alerta_enviada(db, TIPO_SCRAPING, salud.motivo, estado=salud.estado)
    logger.warning("Alerta de scraping enviada (%s): %s", salud.estado, salud.motivo)
    return True


def revisar_y_avisar(db: Session) -> SaludScraping:
    """Evalúa la salud y avisa si hace falta. Punto de entrada del worker."""
    salud = evaluar_salud_scraping(db)
    if salud.requiere_aviso:
        enviar_alerta_scraping(db, salud)
    return salud
