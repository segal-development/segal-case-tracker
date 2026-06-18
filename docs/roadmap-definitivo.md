# Roadmap — qué falta para que el sistema funcione de forma DEFINITIVA

Estado: Junio 2026. Leyenda: ✅ hecho · 🔄 en curso · 🌙 agendado · ⬜ pendiente

---

## A. Scraping e infraestructura (que sea automático y permanente)

- ⬜ **Equipo dedicado always-on** (la pieza permanente): mini-PC/Mac mini en la oficina, enchufado 24/7. Reemplaza el Mac actual.
  - launchd / auto-arranque al energizarse (se recupera tras cortes).
  - health-check con **alerta por mail** si el scraping deja de actualizar (clave para que sobreviva sin nadie mirando).
- 🔄 **Descarga completa de documentos** a GCS: 2025/2026 en curso (~2.585/9.190). Luego bajar `DETAIL_MIN_YEAR` para los años viejos.
- 🔄 **Cobertura completa de movimientos/litigantes**: hoy ~667 causas tienen movimientos/litigantes; el freshness sync los está llenando. Falta completar.
- ⬜ **Scraping per-abogado con segunda clave** (Clave del Poder Judicial), human-assisted captcha (sin 2Captcha):
  - Spike: validar `login_with_token` con la clave de Carla (captcha a mano una vez).
  - Build: flujo de login segunda clave + captcha desde el front + orquestación N abogados + corre en el box residencial (IP-binding).
- ⬜ (Opcional) Spike cloud + proxy ISP estático — solo si se quiere "cero fierros" (ver `spike-cloud-proxy-runbook.md`).

## B. Seguridad / hardening (riesgo ALTO — sube por el per-abogado)

- 🌙 **Rotar contraseña de la DB** (esta noche, ventana overnight — ver `db-rotation-runbook.md`).
- 🔄 **Secret scanning en CI** (gitleaks creado en `.github/workflows/secret-scan.yml` — falta commitear/PR).
- ⬜ **Validación estricta de `ENCRYPTION_KEY`** en prod (sacar padding/truncado). Pre-check: contar filas `encrypted_pjud_password` + validez de la key. **OBLIGATORIO** ahora que guardaremos N claves PJUD.
- ⬜ **Secret Manager / KMS** + **MultiFernet** + comando de re-encriptado (rotación de key maestra sin downtime).
- ⬜ **Smoke autenticado formal** (script read-only: login_ok / list_ok / detail_ok / parse_ok), schedulado en el box + screenshot/HTML redacted en falla.
- ⬜ **`/stats/admin` como señal de salud operativa** + alerta si `last_checked_at` envejece (no reemplaza `/health`).
- ⬜ **Cifrado de sesiones en Redis** (prioridad baja — el scraper ya no usa Redis; solo el path API/worker).
- ⬜ **Runbook de incidente**: rotar key, re-encriptar, purgar Redis, revocar credenciales.

## C. Frontend / producto

- ⬜ **Auth real** (hoy es mock). Se unifica con el login per-abogado (segunda clave). Reemplazar el login falso.
- ⬜ **Vista móvil** (hoy placeholder).
- ⬜ **Test del endpoint `/stats/admin`** (quedó sin test formal — agregar antes de commitear).
- ⬜ **Escritura** (crear causa / nuevo plazo / subir doc): definir si va. Hoy read-only (modales son stubs).
- ⬜ **Code-splitting** (warning de chunk >500kB por recharts) — performance, menor.

## D. Calidad / CI / cierre

- ⬜ **Commitear + PR todo el trabajo** (frontend + backend + scripts + docs) con review. Mucho está sin commitear.
- ⬜ **Tests** para los endpoints nuevos (`/stats/firm`, `/stats/admin`).
- ⬜ **Parser contract tests** con fixtures HTML reales (mitiga fragilidad del scraping ante cambios de PJUD).
- ⬜ **Circuit breaker / retry uniforme** también en detail/download (no solo en listados).

---

## Orden sugerido

1. **Esta noche:** rotar DB.
2. **Post-demo, semana 1:** equipo dedicado + launchd + alerta · pre-check + validación ENCRYPTION_KEY · secret scanning commiteado · smoke autenticado · commitear/PR lo hecho.
3. **Post-demo, semana 2-4:** per-abogado segunda clave (spike → build) · Secret Manager/KMS + MultiFernet · auth real · cobertura de docs/movimientos completa · parser contract tests · resiliencia uniforme.
4. **Backlog:** vista móvil · escritura · Redis encryption · code-splitting.

## Hecho en esta sesión (referencia)
Semáforo per-side · dashboards reales (Supervisor/Productividad/Admin) · Novedades · detalle completo · vista per-abogado · freshness sync + fix de ventana única · descarga de docs activada (foco 2025/26) · endpoints `/stats/firm` y `/stats/admin` · chmod 600 de secretos · workflow de secret-scan · decisión de infra + runbooks (spike cloud, rotación DB, propuesta a Gerencia).
