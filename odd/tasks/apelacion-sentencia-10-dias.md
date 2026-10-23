# Apelación de sentencia definitiva: 10 días, no 5

El motor modela la apelación de la sentencia definitiva como `APELACION_5D` (5 días hábiles, art. 475).
Carla Segal confirmó el 2026-10-23 que el plazo es de 10 días. Con 5 el sistema da por CERRADA una
ventana que sigue abierta: se pierde un derecho irreparable.

## Specs

- S1: Carla Segal, 2026-10-23, a la pregunta "Apelación de sentencia definitiva. El sistema da 5 días.
  Entiendo que el art. 189 vigente da 10 días para sentencias definitivas. ¿Cuál usan?": "1.- 10".
  El plazo de apelación que dispara una sentencia definitiva dura 10 días hábiles (art. 189 CPC;
  el sábado es hábil, confirmado por Carla el 2026-09-29; no hay feriado judicial).
- S2: Se mantiene el IDENTIFICADOR `apelacion_5d` (fila persistida en 787 `case_deadlines`, escala del
  semáforo, ~70 tests, contrato con Sysgal): solo cambian los días (5 -> 10), la base legal
  (-> "art. 189 CPC") y la escala del semáforo (proporcional). El nombre queda como histórico,
  comentado en el código para que nadie lo "corrija" revirtiendo los días.
- S3: Sigue siendo OPCIONAL para el estudio (apelar es un derecho, nunca produce ROJO por sí solo) y
  sin alertas ni correos nuevos.
- S4: Las filas ya vencidas por la regla vieja se recalculan: una fila `expired` cuya nueva fecha
  (10 días) todavía no pasó vuelve a `active`; las que ya pasaron con 10 días quedan `expired`; las
  `superseded` no se tocan.

## Tasks

- [ ] T1 · S1-S4 · worker · motor: días, base legal, escala del semáforo, rótulo y descripción del endpoint; tests
- [ ] T2 · S4 · verificar tras el deploy · recálculo guardado de las causas con plazo de apelación vencido/activo y reinicio del worker de la estación

## Log

- L1: (2026-10-23) Respuesta de Carla por WhatsApp, 15:48: "1.- 10".
- L2: Medido el mismo día en la base real: 8 causas en estado `sentencia` cuyo plazo de apelación el motor
  daba por vencido (5 días) y con 10 días seguía abierto: C-1359-2026 (2º Rancagua, vence 2026-10-23),
  C-4898-2026, C-1786-2026, C-410-2026 (vencen 2026-10-24), C-817-2026 (2026-10-26), C-8085-2025,
  C-15576-2025, C-5960-2026 (2026-10-28). Se avisó a Marcelo para revisión humana inmediata.
- L3: Filas hoy: `apelacion_5d` 645 superseded, 130 expired, 12 active; `legal_basis` "art. 475 CPC"
  (647) y "art. 187/475 CPC" (140). Todo en el motor (deadlines_config.py:95), semáforo
  (semaforo_config.py:90 `SCALE_APELACION_5D`, deadline_semaforo.py:48/128) y descripción del endpoint
  (api/v1/deadlines.py:93).
