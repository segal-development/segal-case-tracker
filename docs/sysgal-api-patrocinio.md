# Sysgal API — `GET /api/sysgal/v1/patrocinio`

Patrocinio/poder status per Sysgal causa. Read-only, pull. Auth: `X-API-Key` (same key as the
other `/api/sysgal/v1` endpoints). No other `/api/sysgal/v1` doc exists, so this one stands alone.

## Query

| Param | Notes |
|---|---|
| `sysgal_causa_id` | Sysgal causa id. Repeatable (`?sysgal_causa_id=1&sysgal_causa_id=2`) or comma list (`=1,2`). Non-integer -> 422. Unknown ids are simply absent from `items`. |
| `rol` + `tribunal` | Must be sent together (else 422). `rol` is normalized like Sysgal ROLs; `tribunal` matches case-insensitively. |
| `actualizado_desde` | ISO datetime. Only causas whose `actualizado_at` is at/after it. |
| `page`, `per_page` | Default 1 / 50, `per_page` max 200. |

With no filter, every Sysgal causa is returned (ordered by `sysgal_causa_id`).

## Response

```json
{"total": 1, "page": 1, "per_page": 50, "items": [{
  "sysgal_causa_id": 102, "case_id": 55, "rol": "C-2-2026", "tribunal": "Juzgado PA",
  "estado": "poder_pendiente", "bloqueado_por_mora": false,
  "poder_acreditado_at": null, "escrito_presentado_at": "2026-10-05T00:00:00",
  "actualizado_at": "2026-11-17T09:00:00"}]}
```

`estado`:
- `sin_causa_pjud`: the Sysgal causa has no linked case yet (`case_id` null).
- `poder_pendiente`: the court ordered acreditar patrocinio y poder (`acreditar_poder_3d`) and there is no compliance after it.
- `poder_acreditado`: that order was complied with (`poder_acreditado_at` = compliance movement), or there is no order, the case is no longer `patrocinio_pendiente` and a firm AB./AP. litigante is on it (date null).
- `escrito_presentado`: in PJUD, none of the above.

`bloqueado_por_mora` is **always `false` for now**: the cuotas source (ITDS endpoint, tasks T1/T2) does not exist yet. Sysgal knows who is moroso and generates the cobro request itself.

`actualizado_at`: latest of the causa's `synced_at`/`sysgal_updated_at` and, when linked, the case's `updated_at`/`last_movement_at`.

## IP restriction

Setting `SYSGAL_API_ALLOWED_IPS` (comma list of IPs/CIDRs; empty = no restriction). Applies to this endpoint only; a non-allowed IP gets 403. The client IP is the first `X-Forwarded-For` hop, then `X-Real-IP`, then the ASGI peer. This is defense in depth: those headers are client-supplied, the API key is the real gate. Production value: ITDS server IP (see `odd/tasks/sysgal-cuotas-poder.md` L4).
