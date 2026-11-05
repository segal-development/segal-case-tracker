# Cache the per-lawyer case scope for a short time
For a plain lawyer every Causas request (list, /cases/summary, /cases/facets) takes ~2.0-2.4 s whatever the chip; an admin in firm mode takes 0.05 s. Cause (explorer, matches the production measurement): the scope (which case ids the lawyer may see) is recomputed in Python on every request.

## Specs
- S1. Measured in production (2026-11-05, in-container TestClient, min of 3): plain lawyer `/cases?sem=<any>` 1.97-2.26 s, `/cases/summary` 2.27-2.31 s, `/cases/facets` 2.07-2.10 s; admin firm-wide 0.04-0.30 s. Goal: a repeated request by the same lawyer within the TTL skips the scope recomputation.
- S2. Cache the result of the expensive scope computation for non-firm-wide users: `resolve_case_scope` (app/api/deps.py:318) and/or `case_ids_for_abogado` (app/services/lawyer_roster.py:771), whichever holds the cost (read both and `_bootstrap_owned_case_ids` first; measure with a quick local timing if useful). The firm-wide `ALL_CASES` path is not cached (it is already cheap).
- S3. SECURITY (scope decides which cases a person sees): the cache key MUST include every input that changes the result: the account identity (lawyer id AND rut) and role, plus any requested `abogado_rut`. Two different accounts must never share an entry. Store an immutable value (frozenset) so no caller can mutate a cached set; callers that need a set get a copy or the frozenset (check every caller still works). Never cache an exception or a partial result.
- S4. TTL from settings: new `CASE_SCOPE_CACHE_SECONDS: int = 60` in app/config.py; `0` disables the cache entirely (the code path must then be exactly today's). Monotonic clock (`time.monotonic`), injectable for tests. Thread safe (a `threading.Lock` around reads and writes; the computation itself may run outside the lock). Bounded size (e.g. 512 entries; evict the oldest). Per process is fine.
- S5. Tests run isolated: an autouse fixture in tests/conftest.py clears the cache before each test, so no existing test sees stale scope. Expose a small `clear_case_scope_cache()` helper for that.
- S6. Strict TDD, tests first, RED seen, in tests/api/test_case_scope_cache.py (use the existing fixtures for lawyers/cases/auth in tests/conftest.py): (a) two requests by the same lawyer within the TTL compute the scope once (count calls of the expensive function via monkeypatch spy); (b) after the TTL (advance the injected clock) it recomputes; (c) two different lawyers never share: lawyer B does not get lawyer A's cases, and each gets exactly what the uncached code gives; (d) a different `abogado_rut` parameter is a different entry; (e) `CASE_SCOPE_CACHE_SECONDS=0` computes every time; (f) admin/auditor firm-wide is unaffected; (g) mutating a returned scope does not change the cached one. Existing tests keep passing untouched.
- S7. Out of scope: rewriting the scope as SQL, indexes, the year-floor LIKE chain, /summary query merging, frontend. Note any of these you see as follow-ups in the report only.

## Tasks
- T1 backend (S2-S6) route: worker. Commit: none (the parent reviews, measures on a copy in production, then commits).

## Log
L1. Marcelo, 2026-11-05: "que veamos donde hay problemas de carga.. ya que a veces demora mucho en cargar cuando filtran haciendo click a esos botones".
L2. Marcelo, 2026-11-05: "Si" to a 60-second per-lawyer cache of the case scope (tradeoff accepted: a newly assigned case can take up to a minute to appear).
