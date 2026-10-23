# Medida prejudicial: sin flujo, plazos ni alertas de juicio ejecutivo

El sistema trata el cuaderno "Medida prejudicial" como juicio ejecutivo (flujo, plazo de
excepciones, plazo fatal, semáforo rojo, "Crítica"). En esas causas el estudio es siempre el
solicitante y no defiende un ejecutivo. Mapa de impacto: explorer 2026-10-22 (ver Log L3).

## Specs

- S1: Carla Segal: "1- si" (el estudio es siempre el solicitante) y "2 en una medida
  prejudicial, jamás" (nunca es el requerido). Toda fila con `procedure = 'Medida prejudicial'`
  (valor único medido: 4.484 filas, sin variantes) es una causa donde el estudio es solicitante.
- S2: Carla Segal: "3- no avanzamos con la demanda que era lo que yo te decía, porque nos pueden
  demandar reconvencionalmente" y "solo terminamos con la medida prejudicial y avanzamos
  dependiendo de la cuantía". Esas filas NO llevan flujo del juicio ejecutivo, plazo de
  excepciones/sentencia/apelación, plazo fatal, semáforo de color, badge "Crítica", recomendaciones
  (abandono, prescripción, apremio), ni "Oportunidades", ni alertas/correos de plazos. NO se vigila
  el art. 280 (decisión del estudio; queda fuera del sistema).
- S3: Marcelo: "Sale arriba Medida prejudicial pero abajo muestra flujo de Flujo del juicio
  ejecutivo entonces eso esta mal porque las medidas prejudiciales somos nosotros los que estamos
  demandando, aparte esas medidas siempre empiezan por Apellido/Acreedor". La pantalla debe decir
  que es una medida prejudicial y no mostrar el flujo ejecutivo.
- S4: La neutralización es por FILA (`procedure`), nunca por ROL: 20 causas tienen una fila
  Principal y otra Medida prejudicial; la Principal no se toca. `procedure` NULL o vacío no se
  neutraliza.
- S5: Los plazos ya guardados de esas filas (323 causas con `excepciones_8d` vencido, 96
  superseded, 9 activos; 29 en rojo, 2 con fatal) dejan de aparecer en paneles, alertas, API
  Sysgal y rotación. Sin borrar historial: no hay plazos manuales ni overrides en esas filas.

## Tasks

- [x] T1 · S1-S5 · worker · backend: helper + early-return neutral en `recompute_case`, supersede de plazos no manuales, corto-circuito de alertas, exclusión en lecturas directas de `case_deadlines`, script de backfill (dry-run por defecto)
- [ ] T2 · S5 · requiere visto bueno · correr el backfill sobre la base real (dry-run primero)
- [ ] T3 · S2-S3 · PR aparte (repo frontend) · `ProcesoStepper`/`CausaDetalle`: rótulo "Medida prejudicial" sin flujo ejecutivo

## Log

- L1: (2026-10-22) Marcelo: "Me dijeron que es grave por que por ejemplo : [captura de C-360-2026,
  15° Juzgado Civil de Santiago, LIRA / BANCO FALABELLA] Sale arriba Medida prejudicial pero abajo
  muestra flujo de Flujo del juicio ejecutivo entonces eso esta mal porque las medidas
  prejudiciales somos nosotros los que estamos demandando, aparte esas medidas siempre empiezan por
  Apellido/Acreedor".
- L2: (2026-10-22 18:01-18:03) Carla Segal, por WhatsApp: "1- si" / "2 en una medida prejudicial,
  jamás" / "3- no avanzamos con la demanda que era lo que yo te decía, porque nos pueden demandar
  reconvencionalmente" / "solo terminamos con la medida prejudicial y avanzamos dependiendo de la
  cuantía".
- L3: Medido: 4.484 filas `Medida prejudicial` (28% de la cartera), 90% ya en gris; patrón del
  caratulado: 0% con institución como demandante (9 de 4.484, 4 son el apellido Santander) contra
  81% en Principal. Un solo productor (`DeadlineEngine.recompute_case` vía
  `_maybe_recompute_deadlines`); lecturas directas de `case_deadlines` que filtran fuga de datos
  viejos: semaforo_panel.py:166, semaforo_alerts.py:185/201-222/383-420, sysgal /excepciones
  (:269-300), /cases/{id}/deadlines, redaccion/detalle.py, emit_ratification_alert. `_write_gris`
  no resetea `en_apremio` ni `semaforo_override*`. No causado por T3c (4.265 de las 4.484 ya
  existían).
- L4: T1 hecha (worker + revisión del diff + suite completa: 3.866 passed; 29 fallos solo en la corrida conjunta, son los tests de logs que dependen del orden, en aislamiento queda 1: test_movement_selects_do_not_scale_with_n, que también falla en main). Estado neutro por fila vía `DeadlineEngine.neutralize_medida_prejudicial`; lecturas directas excluidas con `medida_prejudicial_clause()`; `_dispatch_pending` no se toca (solo envía alertas guardadas con status active: correr el backfill justo después del deploy). Sin verificar en Postgres. Otros cuadernos secundarios (Incidente, Apremio, Tercería: ~1.500 filas) quedan fuera, a conversar con Carla.
