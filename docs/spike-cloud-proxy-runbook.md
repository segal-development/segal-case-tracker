# Spike: validar scraping PJUD en la nube (VPS + proxy ISP estático)

**Objetivo:** responder con datos la ÚNICA pregunta abierta del camino cloud:
¿un único IP residencial/ISP **estático** (que no rota) pasa F5 Shape de forma
**estable y desatendida** para nuestro scraping autenticado de PJUD?

Si pasa → tenemos opción "cero fierros, ~US$10–30/mes, sobrevive a cambios de
personal". Si falla → confirmamos el equipo dedicado, con evidencia.

> Este spike NO compromete nada en producción. Es una prueba aislada de bajo costo.

---

## Por qué este enfoque y no los SaaS

- **VPS auto-alojado** (no Bright Data Scraping Browser / AdsPower / Browserless):
  la sesión y la **Clave Única quedan en NUESTRO VPS**, no en un tercero. Con HTTPS,
  el proxy solo tuneliza: no puede leer las credenciales. Evita la exposición de
  identidad nacional que tienen los servicios SaaS.
- **IP estática (sticky/ISP), no rotativa:** PJUD ata la sesión a la IP de origen.
  Una IP fija es obligatoria; las que rotan rompen la sesión.
- **`launchPersistentContext`:** mantiene cookies/almacenamiento entre corridas →
  "clon del equipo físico" en la nube.

## ⚠️ Punto crítico que los pitches genéricos omiten: la GEO del proxy

El login es con **Clave Única desde Chile**. El IP del proxy **debería ser chileno**
(o, en el peor caso, geográficamente coherente). Un IP de EE.UU. logueándose a
Clave Única es **más sospechoso**, no menos. Los IPs **residenciales/ISP chilenos
estáticos son más escasos y caros** que los genéricos US/EU — esto es parte de lo
que el spike debe validar (disponibilidad + costo + que pase Shape).

---

## Qué provisionar (acción del usuario)

1. **VPS chico** (DigitalOcean / Vultr / AWS Lightsail): 2 vCPU / 4 GB, Linux Ubuntu.
   Costo ~US$12–24/mes (o prorrateado por días del spike).
2. **1 proxy ISP/residencial ESTÁTICO, idealmente con exit en Chile.**
   Proveedores a cotizar: IPRoyal (ISP/static residential), Oxylabs ISP, Bright Data ISP,
   Webshare (ISP). Pedir: **IP dedicada estática, geo Chile, sin rotación.**
   Costo objetivo ~US$5–15/mes por IP.
   - Si NO hay ISP estático chileno asequible → es en sí un hallazgo del spike
     (la nube se complica para nuestro caso).

## Setup técnico (lo prepara el equipo de desarrollo)

1. Clonar el repo en el VPS + venv + `playwright install chromium`.
2. Correr Chromium **headful bajo Xvfb** (display virtual) — `xvfb-run`.
3. Configurar el **proxy fijo** en `BrowserFactory` (Playwright `proxy={server,username,password}`).
4. Usar `launchPersistentContext` con un `userDataDir` persistente.
5. Apuntar `.env` al mismo Cloud SQL (solo lectura/escritura de prueba en causas marcadas).

## Criterio de éxito (medible)

| Check | Éxito |
|---|---|
| Login Clave Única | Completa sin bloqueo (como en el Mac) |
| Scraping de detalle | Devuelve data real (NO 404 en el AJAX de movimientos) |
| Estabilidad | **5–7 días corridos** desatendido sin que Shape corte el IP |
| Sesión | Se mantiene/renueva sin intervención manual |

- **PASA los 4** → camino cloud viable. Migramos producción ahí.
- **FALLA cualquiera** (especialmente el 404 o un corte a los días) → Shape marcó el IP.
  Confirmamos equipo dedicado.

## Costo total del spike

~US$10–30 (un mes de VPS chico + un IP ISP estático). Cancelable al terminar.

## Tiempo

~1–2 h de setup + 5–7 días de observación pasiva.
