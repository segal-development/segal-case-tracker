# Prescripción: demanda no notificada dentro de un año desde su presentación

`_compute_prescripcion` (deadline_engine.py) usa 1 año (pagaré/letra/cheque) o 3 años (resto) desde
`filed_at`; ninguna causa tiene `titulo_tipo`, así que todas caen en 3 años. Medido (2026-10-26, 9.526
activas): 3.339 con `prescripcion_cumplida`; 2.907 ingresadas hace >1 año sin notificación exitosa
publicada, de las cuales solo 1.334 están marcadas hoy.

## Specs

- S1: Carla Segal, 2026-10-23 y 2026-10-26. Respuesta textual: "si no se logro notificar la demanda
  dentro de un año desde su presentación, se puede pedir la prescripción". Antes: "prescripción desde la
  presentación de la demanda (1 año sin notificación)".
- S2: Regla nueva: `prescripcion_cumplida = True` cuando han pasado más de 1 año desde `filed_at`
  (estrictamente: `filed_at + 1 año < hoy`) Y la demanda NO fue notificada dentro de ese año (ninguna
  notificación exitosa de la demanda con fecha entre `filed_at` y `filed_at + 1 año`). Una notificación
  posterior al año NO cuenta. `prescripcion_fecha` = `filed_at + 1 año` en ese caso.
- S3: La regla anterior (1 año título de crédito / 3 años general desde `filed_at`) NO se elimina: Carla
  no se pronunció sobre ella. La marca final es el OR de ambas. Documentarlo como limitación.
- S4: "Notificada" = lo que el motor ya trata como notificación de la demanda (`NOTIFICACION_EXITOSA`
  en deadlines_config.py / `MovementClassifier`), usando la fecha de diligencia (`anchor_date`) y,
  como en excepciones, un "Requerimiento de Pago" no anulado ni "(Paga)" también cuenta como notificada
  (es la notificación efectiva). Ante duda, contar como notificada (no marcar de más).
- S5: Sin `filed_at`: `(False, None)` como hoy. Medida prejudicial sigue neutral.
- S6: Cada comando/texto que muestre la prescripción debe seguir diciendo lo correcto: revisar el texto
  de la recomendación `oponer_prescripcion` en decision_rules.py y que no cite un artículo equivocado.

## Tasks

- [ ] T1 · S1-S6 · worker · motor + tests (RED primero) + textos
- [ ] T2 · S2 · tras deploy · recálculo guardado en la VM con medición antes/después

## Log

- L1: Carla (2026-10-26): "si no se logro notificar la demanda dentro de un año desde su presentación, se puede pedir la prescripción".
- L2: Marcelo: "si vamos con todo".
