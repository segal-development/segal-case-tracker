# Avisar cuando un plazo fatal ya se venció

## Objetivo

Que un abogado se entere de que un plazo fatal se le pasó. Hoy no se entera
nunca: ni de que existía el plazo, ni de que venció.

## El problema medido (2026-10-19)

El 86 % de los requerimientos se detecta después del día 8 (239 de 278). Cuando
eso pasa, `semaforo_alerts._dispatch_pending` no envía nada: solo despacha
mientras el término sigue corriendo. La alerta se crea, queda
`email_sent=false`, y nadie la ve.

| | |
|---|---|
| Alertas `semaforo_rojo` creadas | 257 |
| De esas, enviadas por correo | 35 (18 %) |

El 18 % no es un fallo del correo: es la latencia de detección apareciendo río
abajo. Mientras la detección no sea perfecta —y nunca lo va a ser— va a existir
una cola de plazos descubiertos tarde que hoy son silencio absoluto.

## Alcance

Un plazo entra en el aviso cuando se cumplen TODAS:

1. `deadline_type` es fatal (`excepciones_8d`, `apelacion_5d`).
2. `due_date < hoy`.
3. El `verdict` dice que NO actuamos: `no_cumplido` o `fuera_de_plazo`.
4. La causa está `active`.
5. El vencimiento cae dentro de una ventana de frescura configurable.

### Población medida con ventana de 30 días

| deadline_type | status | verdict | vencidos en 30d |
|---|---|---|---|
| excepciones_8d | expired | no_cumplido | 13 |
| excepciones_8d | no_cumplido | fuera_de_plazo | 6 |
| excepciones_8d | superseded | no_cumplido | 3 |
| | | **total** | **22** |

22 avisos el primer día, repartidos en varios abogados. Volumen sano.

### Queda FUERA, a propósito

- `verdict = cumplido` (1.416) y `registro_tardio` (372): sí actuamos.
- `verdict` vacío (906): no sabemos si actuamos. Avisar sobre lo desconocido es
  ruido, y el ruido mata la credibilidad del aviso. Si se quiere cubrir, primero
  hay que resolver el verdict, no mandar un correo con una duda.
- Todo lo vencido fuera de la ventana: son cientos y no hay nada que hacer con
  ellos. El aviso es para reaccionar, no para hacer arqueología.

## Restricciones (si se violan, se rechaza)

1. **Un aviso por plazo, nunca recurrente.** Reusar el mecanismo de deduplicación
   que ya tiene `semaforo_alerts`. Repetir "se te pasó" todos los días destruye
   la confianza en el canal.
2. **La ventana es variable de entorno, no constante.** Es un número que vamos a
   mover mirando datos reales.
3. **No toca el semáforo.** El color y sus niveles quedan byte a byte iguales:
   este es un canal paralelo, no un nivel nuevo del semáforo.
4. **`_dispatch_pending` conserva su regla actual** para las alertas de término
   vivo. El aviso de vencido necesita su propio despacho, porque por definición
   su término ya terminó.
5. **Un fallo del aviso nunca rompe el ciclo**, igual que el resto de
   `_run_semaforo_alerts_safely`.
6. **Copy en español neutro** (sin voseo), como el resto de los correos.

## Modo TDD

Habilitado. Runner: el venv de Poetry
(`.../segal-case-tracker-VujtO8MO-py3.11/bin/python -m pytest`).
Más `mypy app/core`, que es un paso del CI que ya costó un deploy fallido.

## Tareas

- [x] **T1** — Selección: los plazos fatales vencidos sin presentar, con la
      ventana configurable. Sin efectos: solo la consulta y sus tests.
      Evidencia: RED = `ImportError: cannot import name
      'expired_unfiled_deadlines'`; GREEN = 66 passed en test_semaforo_alerts,
      `mypy app/core` limpio. Commit: T1_SHA.
      Hallazgo: `FATAL_DEADLINE_VALUES` incluye `acreditar_poder_3d`; el alcance
      pide solo excepciones/apelación, así que se deriva como fatales menos
      `PARALLEL_DEADLINE_VALUES` (poder tiene su propia vía, `ratificar_firma`).
      Variable: `SEMAFORO_EXPIRED_WINDOW_DAYS` (30; <=0 sin ventana).
- [ ] **T2** — Alerta y deduplicación: un tipo nuevo, una sola vez por plazo.
- [ ] **T3** — Despacho por correo con su propia regla, independiente del
      término vivo. Copy en español neutro.
- [ ] **T4** — Verificación contra la base real: contar los avisos que saldrían
      antes de encender, y comparar con las 22 medidas.

## Progreso

Documento creado 2026-10-19. Alcance acordado con Marcelo tras medir la
población. Decisión clave: excluir los 906 con verdict vacío.
