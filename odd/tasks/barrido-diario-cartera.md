# Barrido diario de la cartera

**Estado:** diseño en curso · **Creado:** 2026-10-02

## Objetivo

Saber todos los días qué causas se movieron y cuáles no.

## Problema

Una vuelta completa a la cartera tarda ~80 días, así que hoy es imposible
responder "qué se movió hoy". El portal PJUD **no ofrece ninguna señal de
cambio**: no hay feed, ni estado diario, ni búsqueda por fecha de movimiento.
El listado de "Mis Causas" solo trae `rol, tribunal, caratulado, fecha_ingreso,
estado_cuaderno, cuaderno, institucion` — nada que cambie cuando el tribunal
resuelve algo.

## Por qué es alcanzable igual

Medido sobre `~/Library/Logs/segal/worker.log` (2026-10-01 y 10-02):

| Medición | Valor |
|---|---|
| Causas SIN movimientos nuevos | 90% de 1.230 · mediana **1,1 s** |
| Causas CON movimientos nuevos | 10% · mediana **12,8 s** |
| Costo total de causas (parseo → movimientos) | **72 min** para 1.230 |
| Documentos | 1.700 · mediana 3,5 s · **1,9 h** en total |
| Ciclos del scheduler en 31,7 h | **3** (huecos de 5,7 h y 18,1 h) |
| Tiempo activo | **2,8 h de 31,7** = **9%** |

**El cuello no es el costo por causa: es que la estación está parada el 91% del
tiempo.** A 1.230 causas por 2,8 h activas, la cartera completa (~6.816 en
alcance de rotación) son ~15 h de trabajo real. Sin los PDFs en el barrido,
~6-7 h. Media jornada.

No hace falta que PJUD nos diga qué cambió: si barremos todo a diario, lo
sabemos por diferencia.

## Restricciones que NO se tocan

- **Token JWT de documento: vive exactamente 1 hora.** Verificado en un token
  real del log (`iat` 1790943976, `exp` 1790947576 → 3600 s). Si el barrido
  guarda el token y no baja el PDF en el momento, ese PDF no se puede bajar
  hasta que la causa vuelva a pasar por el detalle. `DETAIL_PENDING_DOCS_FIRST`
  existe por esto.
- **PJUD permite UNA sesión activa por IP.**
- La sesión muere a los ~55-60 min (ya hay re-auth).
- Delays anti-detección deliberados: `DETAIL_FETCH_DELAY` 2 s,
  `DOCUMENT_INTER_DELAY` 4 s, horario hábil, cooldown ante rechazo.
  **Acelerar es la forma de quedarnos sin scraping.**

## Alcance autorizado

- [ ] **T1 · Separar la descarga de PDFs del barrido de movimientos**
      (autorizado por Marcelo 2026-10-02). Sin perder PDFs por vencimiento de
      token. Diseño delegado; pendiente de recomendación.
- [x] **T1b · Sacar el modo backfill de la rotación** (2026-10-02). Quitada
      `DETAIL_PENDING_DOCS_FIRST` del plist (repo + instalado). **Se mantienen
      a propósito `DETAIL_BATCH=80` y `SYNC_INTERVAL=2`**: el comentario del
      plist decía "sacar las 3 claves", pero eso restauraría los defaults
      30/4h, que son MÁS LENTOS. Lo que estorbaba era el orden, no el tamaño
      del lote ni la frecuencia. Efecto al reiniciar la estación.
- [ ] **T2 · Cerrar el tiempo inactivo** para que el barrido corra a diario.
      Candidatos: `SYNC_INTERVAL_HOURS`, `MAX_DATA_AGE_HOURS` (guard
      `needs_sync`), `DETAIL_BATCH_SIZE`, `DETAIL_BATCH_MAX_SECONDS`.
      **No autorizado todavía** — depende del diseño de T1.
- [ ] **T3 · Verificar el volumen de notificaciones.** Un barrido completo
      diario puede inundar de mails. Hay tope `NOTIFY_MAX_PER_SYNC = 25` por
      causa, pero no por persona. **Riesgo abierto.**

## Fuera de alcance

- No se toca la configuración anti-detección.
- No se toca el modelo de sesión ni el re-auth.
- La columna "Fecha" del listado (¿ingreso o última gestión?) dejó de ser
  camino crítico: si el barrido es diario, no hace falta la señal de cambio.
  Queda como optimización futura, sin verificar.

## Verificación

Volver a correr las mediciones de arriba sobre el log y comparar:
tiempo activo %, causas por hora, y días estimados para una vuelta completa
(`GET /sync/frescura` ya expone `dias_vuelta_completa`).

## Hallazgos del diseño (2026-10-02)

**1. La estación corre en modo BACKFILL, no en modo monitoreo.** El propio
plist lo dice: *"Remove these 3 keys once the pending-PDF backlog is drained
to return to normal monitoring cadence"*. Las tres claves siguen puestas:
`DETAIL_PENDING_DOCS_FIRST=true`, `DETAIL_BATCH=80`, `SYNC_INTERVAL=2`.
`DETAIL_PENDING_DOCS_FIRST` ordena la rotación por **causas con más PDFs
pendientes**, o sea elige a propósito las más caras. Esa es una causa directa
de los 80 días.

**2. ORDEN INVERTIDO — separar los PDFs ANTES de acelerar la rotación PIERDE
PDFs.** El token vive 1 hora y la única forma de refrescarlo es volver a abrir
el detalle de la causa. Hoy eso ocurre cada ~80 días. Si diferimos la descarga
con la rotación lenta, los PDFs llegan 80 días tarde o no llegan.
**La rotación rápida es prerrequisito de la separación, no al revés.**

**3. El 91% inactivo NO está explicado.** Teoría del guard `needs_sync`
(`MAX_DATA_AGE_HOURS`=4 vs `SYNC_INTERVAL`=2 ⇒ un disparo de cada dos es
no-op): plausible leyendo el código, pero **no verificable en el log** porque
ese mensaje es `logger.debug` y logueamos a INFO. Y no explica los huecos de
5,7 h observados. **Sin instrumentar, cambiar la cadencia es a ciegas.**

## Riesgos abiertos (del diseño)

- **Inundación de mails.** `sync_movements` manda SMTP **bloqueante dentro del
  loop de scraping**. El tope de 25 es por causa, no por persona. Estimado:
  1.000-3.000 mails/día en régimen, y el PRIMER barrido es mucho peor (causas
  con 80 días de atraso vuelcan semanas de golpe). Además ~1 s de SMTP
  serializado por mail le roba ~30-50 min/día al barrido.
- **OJO**: apagar el dispatch (`NOTIFY_MAX_PER_SYNC=0`) hoy **apaga la
  visibilidad del cliente en silencio**, porque `new_movement` NO está en
  `ACTIONABLE_ALERT_TYPES` y por lo tanto no sale en el mail diario. Hay que
  agregar "causas que se movieron" al digest ANTES de apagar nada.
- **Reintentos infinitos**: `Document` no tiene contador de intentos. Un PDF
  que falla se reintenta en cada pasada, para siempre. Con barrido diario eso
  pasa de costo cada-80-días a costo diario.
- **Doble sesión por IP**: nada en `app/` lo impide. El lock es un
  `asyncio.Lock` local de un script, no protege entre procesos.
- **Perfil de tráfico**: pasar de 9% a ~50% de actividad es un cambio que
  ningún dato del repo cubre. Subir en escalones, mirando Shape.

## Línea base (2026-10-02, ANTES de tocar nada)

Medida sobre `~/Library/Logs/segal/worker.log`, 2026-10-01 y 10-02:

- tiempo activo: **2,8 h de 31,7** = **9%**
- ciclos: **3** en 31,7 h (huecos de 5,7 h y 18,1 h; el de 18 h es la pausa
  nocturna de la estación, es esperable)
- causas procesadas: **1.230** · 90% sin movimientos nuevos
- costo por causa: **1,1 s** sin cambios · 12,8 s con cambios
- documentos: **1.700** · 3,5 s cada uno · 1,9 h en total

Repetir estas mismas mediciones tras un día con el cambio, y mirar
`GET /sync/frescura` → `dias_vuelta_completa` y `ritmo_diario`.

## Progreso

- 2026-10-02 — Medido el costo real por causa y por documento sobre el log.
  Hallazgo principal: 9% de utilización. Diseño completo recibido; ver arriba.
  **Pendiente decisión de Marcelo sobre el orden de ejecución.**
