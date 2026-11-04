# Dates of the procedural stages in the case detail
The "Etapa del procedimiento" stepper (frontend ProcesoStepper) shows where a juicio ejecutivo stands but not WHEN each stage was reached. Add the date of each stage already reached.

## Specs
- S1. Request (L1, verbatim): "Agregar las fechas de estas etapas" (a screenshot of the stepper: Mandamiento de ejecución, Notificación de la demanda, Excepciones opuestas (etapa actual), Traslado al ejecutante, Admisibilidad de excepciones, Término probatorio, Citación a oír sentencia, Sentencia, Terminada).
- S2. The stage is derived by `MovementClassifier._classify` (app/services/procedural_classifier.py): it walks the movements oldest first and advances the state forward only; the movement that advances it is the stage's trigger. The date of a stage = `movement_date` of the movement that advanced the state to it. Nothing is stored today and nothing new must be stored: compute it on the fly.
- S3. Backend: extract the walk so the classifier can also return the history without duplicating logic. `classify()` and `_classify()` keep their signatures and results unchanged (existing tests must pass untouched); add a public method `stage_history(movements, today) -> list[tuple[ProceduralState, date]]` (stage in order reached, with the date of the advancing movement as a `date`; safe-fails to `[]` like `classify`). A state that was never reached by an advance (skipped) simply does not appear. Annulled "[Nulo]" movements never advance (same rule as now).
- S4. API: `GET /cases/{case_id}` (get_case in app/api/v1/cases.py, which already loads every movement) returns a new optional field on `CaseDetailResponse`: `stage_dates: dict[str, str]` mapping the stage value (`"mandamiento"`, `"notificado"`, `"excepciones"`, `"traslado_ejecutante"`, `"admisibilidad"`, `"auto_prueba"`, `"citacion_sentencia"`, `"sentencia"`, `"terminada"`) to an ISO date `YYYY-MM-DD`. Default `{}`; additive (old clients keep working). Only the stages above (never rebelde/indeterminate). Do not add queries: reuse the movements already loaded there. A medida prejudicial case returns `{}` (it has no ejecutivo flow: use the same helper the code already uses for that, look at `medida_prejudicial_clause` / `esMedidaPrejudicial` equivalents in app/services/deadline_engine.py).
- S5. Frontend contract (separate PR in segal-case-tracker-front, same task id): the stepper shows `stage_dates[stage]` under each reached stage, formatted dd/mm/yyyy es-CL; stages without a date show nothing.
- S6. Tests first (strict TDD, RED seen): history of a normal flow (dates in order), a skipped stage (absent), a regression attempt (an old-stage movement after a later stage does not move the date), annulled movement ignored, empty list -> [], same-day movements, `classify()` results unchanged on the existing fixtures; endpoint test for `stage_dates`, an empty case, a medida prejudicial and the scope rule (a lawyer cannot read a case outside her scope: the existing behaviour). No N+1 / no extra query (assert the query count is unchanged if the repo has a helper for that).
- S7. Out of scope: storing the dates, the semáforo, deadlines, any frontend change in this repo.

## Tasks
- T1 backend (S1-S4, S6-S7) route: worker. Commit: none (the parent reviews, then commits).

## Log
L1. "Agregar las fechas de estas etapas" (screenshot of the Etapa del procedimiento stepper).

- T1 backend done (uncommitted): stage_history in classifier (_advances extracted), stage_dates on CaseDetailResponse; tests: tests/services/test_stage_history.py, tests/api/v1/test_case_stage_dates.py. Covers S1-S4, S6.
