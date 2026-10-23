# Excepciones: un movimiento publicado dentro del plazo prueba que se presentó en plazo

`_close_excepciones_on_defense` (deadline_engine.py) toma como prueba de presentación el escrito de
`case_escritos` (`fecha_ingreso`) y deja el movimiento "Opone excepciones" como respaldo que "no
puede probar en plazo ni tarde por sí solo" porque su fecha es de PUBLICACIÓN. Eso es cierto para
probar que algo fue TARDE, no para probar que fue EN PLAZO: una resolución publicada el día D implica
que el escrito se presentó el día D o antes. Si hay un escrito tardío y además un movimiento publicado
dentro del plazo, el motor da por perdido un plazo que se cumplió.

## Specs

- S1: Marcelo: "si" a "¿Preparo el arreglo del hallazgo 1 mientras Carla contesta?". Si existe un
  movimiento de oposición de excepciones cuya fecha es <= `due_date` del plazo `excepciones_8d`, el
  plazo se considera CUMPLIDO aunque exista otro escrito posterior (más tardío) en `case_escritos`.
  La fecha de presentación es <= fecha de publicación, así que publicación <= due implica en plazo.
- S2: Lo contrario no se afirma: un movimiento publicado DESPUÉS del due no prueba que sea tardío si
  no hay escrito (comportamiento actual: superseded + veredicto con margen). No cambia.
- S3: NO se toca la regla del sábado ni ningún cómputo del vencimiento (`due_date`): eso espera la
  respuesta de Carla. Este cambio es solo de prueba/evidencia.
- S4: El flag `plazo_fatal_incumplido` y el estado se recalculan solos: C-16984-2025 debe pasar de
  `no_cumplido`/`fuera_de_plazo` a `cumplido` y quedar sin la marca.

## Tasks

- [ ] T1 · S1-S4 · worker · motor: elegir la evidencia más temprana (escrito o movimiento) para juzgar en plazo, en `_close_excepciones_on_defense` y en `_rejudge_closed_excepciones`
- [ ] T2 · S4 · verificar tras el deploy · recompute de C-16984-2025 (14º Juzgado Civil de Santiago, case id 22062) y confirmar el flag

## Log

- L1: (2026-10-23) Revisión de las 3 causas antiguas con plazo perdido. C-16984-2025: plazo
  `excepciones_8d` disparado 2026-01-27, vence 2026-02-05; movimiento folio 8 "Opone excepciones"
  publicado 2026-02-05; `case_escritos` solo tiene un "Opone excepciones" (Demandado) con
  `fecha_ingreso` 2026-10-14 (probable reingreso tras la certificación tardía de la notificación,
  movimientos del 2026-10-09). El motor registró verdict=`fuera_de_plazo`, `verdict_acted_on`=2026-10-14.
- L2: Hallazgo aparte (NO es parte de este cambio): el motor cuenta el sábado como hábil; 6 de las 9
  causas marcadas habrían presentado exactamente el último día si el sábado no cuenta. Pendiente de
  la respuesta de Carla.
