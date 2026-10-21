# Spike: validar scraping PJUD en la nube (VPS + proxy ISP estático)

**Objetivo:** responder con datos la ÚNICA pregunta abierta del camino cloud:
¿un único IP residencial/ISP **estático** (que no rota) pasa F5 Shape de forma
**estable y desatendida** para nuestro scraping autenticado de PJUD?

Si pasa → tenemos opción "cero fierros, ~US$15–40/mes, sobrevive a cambios de
personal". Si falla → confirmamos el equipo dedicado, con evidencia.

> Este spike NO compromete nada en producción. Es una prueba aislada de bajo costo.

**Estado:** no ejecutado. Requiere los cambios de código de la sección 2 y el
aprovisionamiento de la sección 3.

---

## 1. Por qué este enfoque

### 1.1 VPS auto-alojado, no un SaaS de scraping

No usamos Bright Data Scraping Browser / AdsPower / Browserless: la sesión y la
**Clave Única quedan en NUESTRO VPS**, no en un tercero. Con HTTPS el proxy solo
tuneliza, no puede leer las credenciales. Evita exponer identidad nacional a un
proveedor externo.

### 1.2 IP estática, no rotativa

PJUD ata la sesión al IP de origen: una sesión creada desde un IP es rechazada si
los requests salen por otro. Las IP que rotan rompen la sesión en el medio.

### 1.3 La GEO del proxy es parte de la prueba

El login es con **Clave Única desde Chile**. El IP del proxy **debe ser chileno**.
Una Clave Única entrando desde EE.UU. es *más* sospechosa, no menos. Los IP
residenciales/ISP chilenos estáticos son escasos y caros comparados con los
genéricos US/EU.

**Si no se consigue un ISP estático chileno a precio razonable, eso ya es un
resultado del spike:** la nube se encarece o se descarta sin necesidad de
probar nada más.

---

## 2. Pre-requisitos de código (NO existen hoy)

El runbook anterior daba por supuestas tres capacidades que el código no tiene.
Hay que construirlas antes de provisionar nada, porque sin ellas el spike no
puede correr.

| # | Qué falta | Dónde | Tamaño |
|---|---|---|---|
| 1 | **Soporte de proxy en el navegador.** `BrowserFactory.start()` no acepta `proxy=`. Hay que agregar `PJUD_PROXY_SERVER` / `PJUD_PROXY_USERNAME` / `PJUD_PROXY_PASSWORD` y pasarlos a `launch_persistent_context`. | `app/scrapper/pjud/base.py` (~línea 620), `app/config.py` | chico |
| 2 | **Identidad del navegador configurable.** `_CHROME_UA` está fijo en `Macintosh; Intel Mac OS X 10_15_7`. En un VPS Linux, el navegador diría "soy un Mac" mientras el sistema dice "soy Linux": es exactamente el tipo de contradicción que Shape detecta. Hay que permitir sobreescribirlo por settings y usar una identidad coherente con el sistema anfitrión. | `app/scrapper/pjud/base.py:27` y `:630`, `:759` | chico |
| 3 | **Limitar el ciclo a un subconjunto de abogados.** `_active_lawyer_ids_by_staleness` devuelve TODOS los abogados activos. El spike debe tocar exactamente uno (ver 4.1). Hace falta un `SYNC_LAWYER_IDS`. | `app/workers/sync_scheduler.py:183`, `app/config.py` | chico |

> El punto 3 sirve además para el reparto de carga entre dos máquinas, así que
> no es trabajo exclusivo del spike.

Lo que **sí** existe y se aprovecha:

- `PJUD_USER_DATA_DIR` → perfil persistente (`launch_persistent_context`), que es
  lo que convierte al VPS en un "clon del equipo físico".
- `PJUD_SCRAPING_ENABLED` → interruptor por máquina, con el comentario
  *"False where the IP can't authenticate against PJUD (cloud VM)"*.
- `PJUD_BYPASS_SYSTEM_DNS` → **dejar en `false`**. El bug del resolver es de
  macOS (`mDNSResponder`); en Linux no aplica y agregar la flag solo suma
  superficie de fingerprint.

---

## 3. Aprovisionamiento (acción del usuario)

1. **VPS chico**: 2 vCPU / 4 GB, Ubuntu LTS. DigitalOcean / Vultr / AWS Lightsail.
   ~US$12–24/mes, prorrateable por los días del spike.
2. **1 proxy ISP/residencial ESTÁTICO con salida en Chile.**
   Cotizar en: IPRoyal (ISP/static residential), Oxylabs ISP, Bright Data ISP,
   Webshare ISP. Pedir explícitamente: **IP dedicada, estática, geo Chile, sin
   rotación.** Objetivo ~US$5–15/mes.

**Antes de pagar, confirmar con el proveedor por escrito:**

- [ ] El IP es **dedicado** (no compartido con otros clientes)
- [ ] El IP **no rota** — ni por tiempo, ni por cantidad de requests
- [ ] La salida es **Chile** (pedir el IP concreto y verificarlo)
- [ ] Es **ISP/residencial**, no datacenter disfrazado
- [ ] Se puede **cancelar dentro del mes** sin penalidad

---

## 4. Controles de riesgo (leer antes de ejecutar)

### 4.1 Nunca usar una cuenta que la estación esté scrapeando

**Este es el riesgo serio del spike.** Cada abogado tiene su propia cuenta de
PJUD. Si la estación y el VPS entran con la MISMA cuenta desde dos IP distintas
al mismo tiempo, eso es por sí solo una señal de alarma para Shape — y el daño
no sería al spike, sería a la cuenta real de un abogado en producción.

**Procedimiento obligatorio:**

1. Elegir **un solo abogado, de cartera chica**. Candidatos: Silvia Barros
   (9 causas) o Valentina Araya (132 causas). Cartera chica = menos exposición y
   una vuelta completa rápida.
2. **Excluir a ese abogado de la estación** durante todo el spike (vía
   `SYNC_LAWYER_IDS` en la estación, el mismo setting del punto 2.3).
3. Verificar en el log de la estación que efectivamente dejó de tocarlo **antes**
   de arrancar el VPS.

### 4.2 Base de datos

El VPS apunta al mismo Cloud SQL. Como el abogado del spike está excluido de la
estación, no hay escritura concurrente sobre las mismas causas. Las corridas del
spike **van a aparecer en `sync_history`**: tenerlo en cuenta al leer las
alertas de salud durante esos días.

### 4.3 Qué NO hacer

- No apuntar el spike a la cartera de Carla (5.698 causas, 38% del estudio).
- No correr el spike y la estación sobre el mismo abogado "para comparar".
- No dejar el VPS andando después del veredicto: se cancela.

---

## 5. Setup técnico

1. Clonar el repo en el VPS, `poetry install`, `playwright install chromium`.
2. Instalar Xvfb: `apt install xvfb`.
3. Correr Chromium **headful bajo display virtual**: `xvfb-run -a <comando>`.
   Headful es obligatorio — el modo headless es una de las dos señales que
   Shape combina para bloquear.
4. `.env` del VPS:
   ```
   PJUD_PROXY_SERVER=http://<ip>:<puerto>
   PJUD_PROXY_USERNAME=<usuario>
   PJUD_PROXY_PASSWORD=<password>
   PJUD_USER_DATA_DIR=/opt/segal/chrome-profile
   PJUD_BYPASS_SYSTEM_DNS=false
   PJUD_SCRAPING_ENABLED=true
   SYNC_LAWYER_IDS=<id del abogado elegido>
   ```
5. Verificar la geo del IP antes de tocar PJUD:
   `curl --proxy ... https://ipinfo.io/json` → debe decir `"country": "CL"`.

---

## 6. Línea base contra la que se compara

Medido en la estación el **2026-10-21**, después del arreglo de DNS (PR #384):

| Métrica | Estación (referencia) |
|---|---|
| Errores de red por causa | **0,000** (0 errores / 104 causas) |
| `ShapeChallenge` | **0** |
| `reauth_failed` | **0** |
| Paneles cargados OK | 26 |
| Velocidad, cartera chica | 9–12 s/causa |
| Velocidad, cartera grande | 22–30 s/causa |

El spike no tiene que empatar la velocidad —el VPS sale por un proxy, va a ser
más lento y está bien—. Tiene que empatar **la ausencia de bloqueos**.

---

## 7. Checklist de ejecución

### Día 0 — Puerta de entrada (~2 h)

- [ ] Proxy contratado y `ipinfo.io` confirma `country: CL`
- [ ] Repo + Playwright + Xvfb instalados en el VPS
- [ ] Abogado del spike **excluido de la estación** y verificado en el log
- [ ] `.env` cargado, `PJUD_BYPASS_SYSTEM_DNS=false`
- [ ] **Login de Clave Única completa sin bloqueo**

> **Si el login falla acá, el spike terminó.** Shape bloqueó en la puerta.
> No hay nada que observar 7 días. Cancelar el proxy y el VPS: costo real,
> menos de US$5.

### Día 1 — Primera vuelta

- [ ] Scraping de detalle devuelve datos reales (**no 404 en el AJAX de movimientos**
      — ese 404 es la firma conocida de Shape, no un error de red)
- [ ] ≥ 50 causas leídas
- [ ] `ShapeChallenge` = **0**
- [ ] `reauth_failed` = **0**
- [ ] Anotar segundos por causa (para la comparación de costo, no de aprobación)

### Días 2 a 7 — Estabilidad desatendida

Una medición por día, sin tocar nada entre medio. **Desatendido es parte de lo
que se prueba**: si hay que intervenir a mano, ya falló.

- [ ] D2 · `ShapeChallenge` = 0 · sesión viva · IP sin cambiar
- [ ] D3 · ídem
- [ ] D4 · ídem
- [ ] D5 · ídem
- [ ] D6 · ídem
- [ ] D7 · ídem

Comando de medición diario (ajustar la ruta del log):

```bash
LOG=/var/log/segal/spike.log
echo "=== $(date +%F) ==="
echo "  causas leidas  : $(rg -c 'detail scraped' "$LOG")"
echo "  ShapeChallenge : $(rg -c 'ShapeChallenge' "$LOG")"
echo "  reauth_failed  : $(rg -c 'reauth_failed' "$LOG")"
echo "  404 movimientos: $(rg -c '404' "$LOG")"
rg -n 'Sync complete' "$LOG" | tail -3
curl -s --proxy "$PJUD_PROXY_SERVER" https://ipinfo.io/json | rg '"ip"|"country"'
```

---

## 8. Criterio de corte temprano

Cortar **el mismo día**, sin esperar los 7, si ocurre cualquiera de estos:

| Señal | Qué significa |
|---|---|
| Login falla en D0 | Shape bloquea el IP de entrada |
| 404 en el AJAX de movimientos | Firma conocida de Shape, no es red |
| `ShapeChallenge` > 0 en cualquier día | El IP quedó marcado |
| El IP cambió sin que lo pidiéramos | El proveedor mintió sobre "estático" |
| Hizo falta intervención manual | No es desatendido, que es el requisito |

Cortar temprano **es un resultado válido y ahorra dinero.** El spike no está
para confirmar una esperanza; está para matar la duda rápido y barato.

---

## 9. Veredicto

**PASA** — los 7 días con `ShapeChallenge` = 0, sesión desatendida, IP estable:

→ El camino cloud es viable. Se migra producción. El estudio no compra ni
depende de ningún equipo físico, ni ahora ni cuando este se muera. El reparto
de carga entre máquinas se vuelve trivial: se levanta una segunda VM.

**FALLA** — cualquier corte de la sección 8:

→ Se confirma el equipo dedicado (Opción A de
`propuesta-infraestructura-scraping.md`), **ahora con evidencia medida** en
lugar de una recomendación. Se compra el mini-PC o el Mac mini y se cierra la
discusión.

Las dos salidas son útiles. No hay forma de perder este spike salvo no correrlo.

---

## 10. Costo y tiempo

| | |
|---|---|
| Código previo (sección 2) | ~medio día de desarrollo, reutilizable |
| VPS + proxy | **US$10–30**, una vez, cancelable |
| Setup | ~2 h |
| Observación | 5–7 días pasivos |
| Riesgo para producción | Ninguno, si se respeta la sección 4 |
