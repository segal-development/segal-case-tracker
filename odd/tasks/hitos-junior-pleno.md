# Hitos: one Junior and one Pleno per lawyer and causa
A lawyer promoted from Junior to Pleno must be able to file a Pleno hito on a causa where he already has a Junior hito. A second hito of the SAME nivel on the same causa stays blocked.
Backend: /Users/marcelo/Projects/segal-case-tracker.

## Specs
- S1. Marcelo (18-11-2026, verbatim): "Gonzalo Caldeorn antes era Junior y ahora es Pleno, entonces subio un hito con rut : 15.326.356-6 C-1588-2022 y ahora debe hacer lo mismo para esa misma causa pero como Pleno, que podemos hacer ahi?" and "puede pasar con otras causas de Gonzalo, porque subio de Junior a Pleno".
- S2. Today the duplicate key is (lawyer_id, rol_causa, causa via `_causa_key(descripcion)`, tribunal with tolerant collision) regardless of hito type (`_hito_existente` in app/api/v1/hitos.py, used by create, update, the public form endpoints and the `/verificar` previews; the Excel importer has its own existing3/existing4 sets). Only `rechazado` does not block.
- S3. Decision (Marcelo, 18-11-2026, option A): the key adds the nivel of the hito type (`hito_tipos.nivel`, 'junior' | 'pleno'). A Junior hito no longer blocks a Pleno hito on the same causa and vice versa; two hitos of the same nivel on the same lawyer + causa (+ tolerant tribunal) still collide exactly as today, with the same 409 message. Everything else unchanged: rechazado never blocks, other lawyers never collide, closed period rules, the "otro aprobado" approval check (scoped the same way by nivel).
- S4. Applies to every path that uses the rule: manual create, update (changing hito_tipo_id re-checks with the new nivel), both public forms and both `/verificar` previews, and the Excel importer.
- S5. Prod case: lawyer 19, hito 1313 (aprobado, junior_h1_conversion, 15.326.356-6, C-1588-2022, 2º Juzgado Civil de Concepción) must allow a new pleno_* hito for the same causa.

## Tasks
- T1 S2-S5 nivel-aware duplicate rule with tests — route: worker.
- T2 PR, merge, deploy, check the prod case with a dry API call — route: parent.

## Log
- L1. Marcelo's report, 18-11-2026 (quoted in S1).
- L2. Marcelo chose option A: one Junior + one Pleno per lawyer and causa (S3).
