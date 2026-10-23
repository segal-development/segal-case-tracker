# Plazos fatales perdidos: que no desaparezcan de los contadores

Cuando se pierde un plazo fatal el motor lo marca `Case.plazo_fatal_incumplido = True` y deja
`next_deadline_at` en NULL. Casi todos los contadores leen `next_deadline_at`, así que la causa
desaparece justo cuando ocurrió lo peor. Auditoría de código 2026-10-21; mapa de consumidores
2026-10-23 (ver Log L3).

## Specs

- S1: Marcelo: "Que un plazo fatal perdido no desaparezca de los contadores, y que avise" (se
  aprobó con "si dale"). Una causa con `plazo_fatal_incumplido = True` debe contarse y verse en el
  tablero de Riesgo, en la Agenda y en el menú lateral, como un indicador APARTE de "Vencidos"
  (el semáforo mide acción pendiente; el cumplimiento es otro eje).
- S2: El aviso ya existe: la alerta `plazo_vencido` (scan `run_semaforo_alerts`, ventana de 30
  días, dedup por plazo y destinatario). NO se crea un canal nuevo. Hoy 6 de 9 causas marcadas
  recibieron esa alerta con correo; las 3 restantes vencieron hace más de 30 días.
- S3: Nada nuevo en Sysgal (cambio de contrato externo) ni en el correo diario (a quién le llega
  es decisión del estudio).
- S4: Las filas de medida prejudicial nunca llevan el flag (están neutras): no cambia nada para ellas.

## Tasks

- [ ] T1 · S1 · worker · backend: conteo de causas con el flag en `firm_risk_board` (`riesgo_totals`), en el summary de `/calendar` (`incumplido_count`) y orden de "top críticos"; tests que pinean las formas
- [ ] T2 · S1 · PR aparte (repo frontend) · tile en Riesgo, KPI en Agenda, badge del menú lateral y conteo server-side en lugar de `incumplidasAll`
- [ ] T3 · revisión humana · las 3 causas antiguas sin aviso: C-16984-2025 (hace 260 días), C-6216-2026 (87), C-578-2026 (90)

## Log

- L1: (2026-10-23) Marcelo: "si dale" a "¿Arranco con el punto 1?": "Que un plazo fatal perdido
  no desaparezca de los contadores, y que avise."
- L2: Medido: 9 causas con `plazo_fatal_incumplido`, las 9 con `next_deadline_at` NULL y semáforo
  verde; ninguna con más de un cuaderno; 0 plazos `no_cumplido` sin la marca. Antigüedad del plazo
  perdido en días: 15, 18, 22, 23, 27, 30, 87, 90, 260.
- L3: Mapa (explorer): leen `next_deadline_at` y por eso NO muestran un fatal perdido:
  lawyer_roster.py:656-660 (tile Riesgo), :727-739 (top críticos), calendar.py:253/282/331-334
  (items, overdue_count, fatal_count), Agenda.tsx:221, Dashboard.tsx:488, Sidebar.tsx:374-379,
  daily_agenda.py:137, sysgal/plazos.py:73. Sí lo muestran: `GET /cases?incumplido=true`,
  `PlazoIncumplidoBadge`, la tarjeta "Incumplidos" de Plazos.tsx (limitada a 6.000 causas por
  MAX_PAGES), y el panel/alerta `plazo_vencido`. Presupuesto de round-trips del recompute
  (`test_roundtrips_per_case.py`) no se toca: el conteo va fuera del recompute.
