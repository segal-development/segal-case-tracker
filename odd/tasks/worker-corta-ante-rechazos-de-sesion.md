# Worker: stop the cycle when PJUD keeps rejecting fresh sessions

On 2026-10-26 the office egress IP started getting `sessionN.php` -> 404 while the login page still loads
(200). Over a hotspot IP the same POST returns 302, so PJUD rejects by source IP. The worker kept going:
login "succeeds", the very next request is "Not authenticated after session restore", and it re-logs in
for the NEXT lawyer. Measured: 90 session rejections and 35 logins in two hours, against 1 rejection a day
before 2026-10-23 (17 on 10-23). The existing block handling only covers `ShapeChallengeError`
(0 occurrences today); this failure looks like an expired session, so it is never treated as a block.

## Specs

- S1: Marcelo, 2026-10-26: "Preparo ese ritmo reducido ahora" -> goal: do not burn a clean IP again.
- S2: In `sync_all_lawyers` (app/workers/sync_scheduler.py), count CONSECUTIVE lawyer work units whose
  failure is a session rejection that happened right after a fresh re-authentication (the
  "Not authenticated after session restore" / `SessionNotAuthenticatedError` path where `_reauth` already ran
  once). A success, a skip or a fresh-skip resets the counter. Other failures (network, DB, bad
  credentials) neither count nor reset it.
- S3: When the counter reaches `SYNC_AUTH_REJECT_TRIP` (new setting in app/config.py, default 3), stop the
  cycle: log one ERROR with the lawyer ids involved, do not touch the remaining lawyers, and report them in
  the final results as `aborted` (new key, count of lawyers not attempted).
- S4: Cooldown: after tripping, the next scheduled cycles do nothing but log INFO until
  `SYNC_AUTH_BLOCK_COOLDOWN_MINUTES` (new setting, default 90) has passed. In-memory state is acceptable
  (a process restart clears it; document that).
- S5: The new settings must be overridable from the environment like the others. No change to the existing
  Shape handling, to rate limits, to the email/alert pipeline, or to what a successful cycle does.

## Tasks

- [ ] T1 · S2-S5 · worker · breaker + cooldown + tests (RED first)

## Log

- L1: Marcelo (2026-10-26): "Preparo ese ritmo reducido ahora" -> "si". After measuring, the cause is not volume
  but the re-login loop against a rejecting IP; the breaker replaces the "lower the rate" idea.
