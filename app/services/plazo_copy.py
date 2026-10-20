"""Text of a semaforo alert (title + message). Neutral Spanish, tuteo (tu).

Pure. The rules this copy keeps:

* Absolute dates only, never "hoy" / "mañana": the text is stored on the Alert
  and may be re-sent by a later run, when "hoy" would be a lie.
* A plazo whose term is not confirmed is never worded as if it were. It says
  "SIN CONFIRMAR", lists every possible reading with its own date, says where
  the missing fact lives, and tells the lawyer to work against the nearest
  live date meanwhile. The confirmed voice ("Vence el ...", "Último día del
  plazo ...") is reserved for a term somebody confirmed.
"""

from __future__ import annotations

from datetime import date

from app.core.deadlines_config import DEADLINE_DISCLAIMER, DEADLINE_LABELS
from app.core.semaforo_config import EXCEPCIONES_TERM_PLACE
from app.services.deadline_semaforo import EXCEPCIONES, PODER, Assessment, Reading


def _d(value: date) -> str:
    return value.strftime("%d-%m-%Y")


def _unconfirmed_excepciones(a: Assessment, tribunal: str) -> tuple[str, str]:
    earliest = a.earliest_live_due
    lines = [
        "Plazo para oponer excepciones: SIN CONFIRMAR.",
        f"Tribunal: {tribunal}.",
        "No sabemos cuál plazo corre en esta causa: depende del lugar del "
        "requerimiento, que no consta en el PJUD (solo figura en el estampado "
        "del receptor). Puede ser uno de estos dos:",
    ]
    for reading in a.readings:
        verb = "vence" if reading.level is not None else "venció"
        place = EXCEPCIONES_TERM_PLACE[reading.term_days]
        lines.append(
            f"- {reading.term_days} días hábiles, si {place}: {verb} el {_d(reading.due_date)}."
        )
    lines.append(
        "Mientras no se confirme, considera como límite la fecha más cercana "
        f"aún vigente: {_d(earliest)}."
    )
    lines.append(f"Día {a.day} contado desde el {_d(a.triggered_at)}.")
    lines.append(DEADLINE_DISCLAIMER)
    return "\n".join(lines), _d(earliest)


def _single_reading_lines(a: Assessment, reading: Reading) -> list[str]:
    if a.day >= reading.term_days:
        due = f"Último día del plazo: {_d(reading.due_date)}."
    else:
        due = f"Vence el {_d(reading.due_date)}."
    return [
        due,
        f"Día {a.day} de {reading.term_days}, contado desde el {_d(a.triggered_at)}.",
    ]


def build_alert_text(a: Assessment, rol: str, tribunal: str) -> tuple[str, str]:
    """``(title, message)`` for the alert of ``a`` at its current level.

    Raises ValueError when ``a`` has no live level: a plazo that is over has no
    alert, and inventing copy for it would be an alarm about nothing.
    """
    if a.level is None:
        raise ValueError("no live level: this plazo is over, nothing to announce")
    tag = a.level.value.upper()

    if a.deadline_type == EXCEPCIONES and not a.confirmed:
        message, nearest = _unconfirmed_excepciones(a, tribunal)
        title = (
            f"{tag} · Excepciones, causa {rol}: plazo sin confirmar "
            f"(podría vencer el {nearest})"
        )
        return title, message

    reading = a.live_readings[0]
    if a.deadline_type == EXCEPCIONES:
        title = f"{tag} · Excepciones, causa {rol}: vence el {_d(reading.due_date)}"
        lines = [
            f"Plazo para oponer excepciones: {reading.term_days} días hábiles "
            f"({EXCEPCIONES_TERM_PLACE[reading.term_days]}).",
            f"Tribunal: {tribunal}.",
            *_single_reading_lines(a, reading),
        ]
    elif a.deadline_type == PODER:
        title = f"{tag} · Ratificación de firma, causa {rol}: vence el {_d(reading.due_date)}"
        lines = [
            "El tribunal ordenó ratificar la firma / acreditar patrocinio y poder "
            f"en {reading.term_days} días hábiles.",
            f"Tribunal: {tribunal}.",
            *_single_reading_lines(a, reading),
            "Apercibimiento: tener por no presentado el escrito; si es el de "
            "excepciones, se pierde la defensa.",
        ]
    else:  # pragma: no cover - assess() returns None for any other type
        raise ValueError(f"no copy for {a.deadline_type!r}")
    lines.append(DEADLINE_DISCLAIMER)
    return title, "\n".join(lines)


def build_vencido_text(
    deadline_type: str,
    due_date: date,
    verdict: str,
    rol: str,
    tribunal: str,
    *,
    filed_on: date | None = None,
) -> tuple[str, str]:
    """``(title, message)`` for a fatal plazo that expired with no filing from us.

    Absolute dates only (the text is stored and may be read days later). The
    wording follows what the verdict proves: ``no_cumplido`` says we found no
    filing; ``fuera_de_plazo`` says the filing is dated after the deadline,
    and in both cases asks the lawyer to check the expediente, because the
    verdict is derived from published movements, not from the filing itself.

    ``filed_on`` is the FILING date of the escrito that produced the verdict
    (``case_escritos.fecha_ingreso``), when that is where the evidence came from.
    Then the filing is known, so the text states its date and never claims that
    nothing was found.
    """
    label = DEADLINE_LABELS.get(deadline_type, deadline_type)
    title = f"VENCIDO · {label}, causa {rol}: venció el {_d(due_date)}"
    if verdict == "fuera_de_plazo" and filed_on is not None:
        finding = (
            f"El escrito se presentó el {_d(filed_on)}, después del vencimiento del plazo."
        )
    elif verdict == "fuera_de_plazo":
        finding = (
            "El escrito aparece registrado con posterioridad al vencimiento del plazo."
        )
    else:
        finding = "No encontramos un escrito presentado dentro del plazo."
    lines = [
        f"{label}: el plazo venció el {_d(due_date)}.",
        f"Tribunal: {tribunal}.",
        finding,
        "Revisa el expediente para confirmar el estado de la causa y definir "
        "los pasos a seguir. Este aviso se envía una sola vez.",
        DEADLINE_DISCLAIMER,
    ]
    return title, "\n".join(lines)
