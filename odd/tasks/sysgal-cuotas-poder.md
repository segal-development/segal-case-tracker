# Sysgal cuotas gate the acreditación de poder
Clients behind on cuota 1 or 2 get their escrito filed anyway, but the acreditación de poder waits for payment or an authorized liberación. Sysgal shows the patrocinio/poder status.
Backend: /Users/marcelo/Projects/segal-case-tracker. Depends on the ITDS cuotas endpoint (not delivered yet).

## Specs
- S1. ITDS request (17-11-2026, verbatim excerpt): "Necesitamos crear en Sysgal una pantalla para revisar si cada cliente tiene sus cuotas al día antes de presentar el patrocinio: si el cliente está moroso, no se presenta la defensa hasta que se ponga al día." They add an `api_sync` endpoint "con una fila por cuota de cada contrato: contrato, cliente (nombre y RUT), número de cuota, vencimiento, estado (pagada / pendiente / morosa), monto, abogado y cobrador", and ask us for a patrocinio-status API (identifier, states, auth, pull vs push).
- S2. Carla (17-11-2026 16:41, verbatim): "El escrito se presenta. la alarma se genera y queda supeditado la acreditación de poder. la cuota debe estar pagada antes de la acreditación de poder en la causa. debemos tener una alarma que genere la solicitud de cobro"
- S3. Carla (16:44, verbatim): "Lo bloquea hasta la autorización de liberación"
- S4. Carla (16:45, verbatim): "solo aplica respecto de clientes que se encuentren en el pago de su numero de cuota 1 y 2"
- S5. Moroso = Sysgal's own state (Marcelo, 17-11-2026: "El mismo sistema Sysgal tiene eso ya listo y sabe cuando es moroso").
- S6. When the poder is still blocked for mora and the `acreditar_poder_3d` plazo is about to expire: option A (Marcelo, 17-11-2026) — the system alerts Carla 1 business day before so she decides whether to liberate it.
- S7. Patrocinio-status API for Sysgal under the existing `/api/sysgal/v1` (`X-API-Key`), keyed by Sysgal causa id (also ROL + tribunal), pull with `actualizado_desde`, restricted to ITDS's server IP. States: escrito presentado / poder pendiente (bloqueado por mora | libre) / poder acreditado; sin_causa_pjud when not yet in PJUD.
- S8. Open with ITDS: cuota number in gestión per contract, last-updated date and refresh frequency (stale MOROSO would block a client who already paid), Sysgal causa id per contract, `{success, data}` envelope, pagination, server IP.

## Tasks
- T1 S2-S5, S8 consume ITDS cuotas endpoint (store per client: cuota nº, estado, actualizado) — route: worker, after ITDS delivers.
- T2 S2-S4, S6 gate + alarms: cobro alarm, poder block until payment/liberación (reuse liberación), alert Carla 1 day before `acreditar_poder_3d` expires — route: worker.
- T3 S7 patrocinio-status endpoint + IP restriction — route: worker.
- T4 PRs, deploy, check with ITDS — route: parent.

## Log
- L1. ITDS message to the team, 17-11-2026 (quoted in S1).
- L2. Carla's answers 16:41-16:45 (S2-S4). Marcelo: moroso = Sysgal state (S5); option A for the poder deadline (S6); restrict the API by IP (S7).
