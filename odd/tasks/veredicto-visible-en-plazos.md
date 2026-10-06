# Veredicto de plazos visible en la pantalla /plazos

## Objetivo

Que Dirección Jurídica (Carla, rol admin) vea el veredicto calculado de los
plazos desde la aplicación, sin depender de un Excel que queda viejo al día
siguiente ni de la API de Sysgal, que exige `X-API-Key` y está hecha para
consumo máquina a máquina.

## Problema

La ruta `/plazos` ya existe y Carla la ve (`App.tsx:145`, gate
`role === "admin" || "auditor"`). Tiene tarjetas **Cumplido** e
**Incumplido**. Pero lee la columna vieja `status` — el marcado MANUAL — vía
`GET /cases/deadlines/audited`.

Medido en QA el 2026-10-06:

| columna `status` (lo que se ve hoy) | columna `verdict` (lo calculado) |
|---|---|
| cumplido **41** | cumplido **1.266** |
| no_cumplido **0** | registro_tardio **340** |
| | fuera_de_plazo **31** |
| | no_cumplido **5** |

La pantalla muestra el 2,5% del dato. La palabra `verdict` no aparece ni una
vez en todo `src/` del front, y ningún response del backend la expone.

## Por qué

El veredicto se recalcula solo cuando entra información nueva de PJUD. Un
Excel es una foto; la pantalla es el dato vivo. Y hoy la única vía al dato
real para un humano es pedirle a alguien que corra un script.

## Restricción que manda sobre el diseño

**No exponer `fuera_de_plazo` como una acusación.** El atraso se mide contra
la fecha en que PJUD PUBLICÓ el escrito, no contra la fecha en que se
presentó: los escritos no traen `Diligencia:` (0 de 3.270 revisados) y PJUD
publica con ~4 días de atraso. `DEADLINE_PUBLICATION_MARGIN_DAYS = 5` ya
absorbe ese ruido en `registro_tardio`.

Decisión tomada: los 340 `registro_tardio` cuentan como **Cumplido** en las
tarjetas. No podemos afirmar que se presentaron tarde. El detalle por fila
sigue visible. Repetir en la app el error que casi se cometió en el reporte
(acusar a 371 abogados cuando los defendibles son 16) está explícitamente
fuera de contrato.

## Alcance autorizado

- Backend: exponer `verdict` y `verdict_acted_on` en el endpoint que ya
  existe, y ampliar su filtro para devolver las filas con veredicto.
- Front: alimentar las tarjetas con el veredicto y mostrar los tramos con su
  advertencia de medición.
- Fuera de alcance: tocar el motor de plazos, el cálculo del veredicto, la
  API de Sysgal, o la regla del auto de prueba (ya se verificó que no tiene
  el bug que se le atribuía — ver PR #327).

## Modo TDD

Habilitado. Backend: `poetry run pytest -m "not integration" -n 4`.
Front: `npm run test` (vitest), más las cuatro puertas `tsc` → `eslint
--max-warnings 0` → `vitest` → `npm run build`.

## Entrega

Dos repositorios, dos PRs. Previsión: ~120 líneas backend, ~220 front.
Bajo el presupuesto de ~400 líneas por slice, sin encadenado.

## Tareas

- [x] **T1 · backend** — Endpoint NUEVO `GET /cases/deadlines/verdicts`.
      Ruta: inline (un archivo no trivial, ya comprendido).
      Checks: 6 tests nuevos, 3 mutaciones cazadas, suite 2876 passed.
      Commit: (ver abajo)

      **Corrección de diseño sobre el plan original.** El plan decía ampliar
      `/cases/deadlines/audited`. Está mal: hay dos invariantes explícitas y
      testeadas que eso rompería —
      `test_audited_excludes_engine_marked.py` fija que esa lista contiene
      SOLO auditorías humanas (`marked_at` seteado), y
      `TestAuditorMarksWin::test_audited_row_is_not_given_a_verdict` fija que
      el motor nunca le pone veredicto a una fila que un humano marcó. O sea
      `status` y `verdict` son excluyentes por diseño: la marca humana gana.
      Endpoint separado entonces, y en pantalla quedan separados también.

      Verificado contra QA: 1.642 filas, 19 ms de ejecución en el servidor
      (tabla de 4.510 filas, seq scan, no hace falta índice). Los 776 ms que
      se miden desde acá son el proxy y el transfer, no la base.
      `resolve_case_scope` da `ALL_CASES` a `auditor` Y `admin`
      (`deps.py:339`), así que Carla ve todo el estudio — hay un test que
      fija eso, porque es lo que puede romperse en silencio y dejarla
      mirando una pantalla vacía.

- [ ] **T2 · front** — Alimentar las tarjetas desde el veredicto: Cumplido
      (`cumplido` + `registro_tardio`), Revisar (`fuera_de_plazo`), Sin
      presentar (`no_cumplido`). Dentro de Revisar, separar por días de
      atraso (>15 / 6-15) con la advertencia de medición.
      Ruta: delegada (Plazos.tsx 960+ líneas + hook + tests).
      Checks: las cuatro puertas.

      Tramos ya verificados contra QA, suman exacto: Cumplido 1.606
      (`cumplido` 1.266 + `registro_tardio` 340) · Revisar 16 (atraso > 15
      días) · Dudosas 15 (atraso 6-15 días) · Sin presentar 5
      (`no_cumplido`) = 1.642. Revisar=16 y Dudosas=15 coinciden clavado con
      las hojas del Excel que ya se le pasó a Carla.

## Progreso

- T1 cerrada. Suite backend 2876 passed, 1 xfailed.
- Siguiente: T2 (front).
