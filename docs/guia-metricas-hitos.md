# Guía de métricas de hitos — fuente de verdad para el detector

> **Propósito.** Referencia canónica de los hitos H1 (movimiento útil) del área de
> tramitación, para construir el **detector de hitos desde PJUD**. Consolida dos
> fuentes del cliente y deja explícitas las reglas que el motor de detección debe
> implementar. Cualquier cambio del catálogo se refleja acá primero.
>
> **Fuentes:**
> - *Guía de Procedimiento por Matriz Procesal* — Modelo Operacional **V9** (V7.1, Mayo 2026). Define los hitos **por matriz** y su condición de devengo.
> - *Modelo Operacional* **V11** §7.6 (tabla hito → ETAPA/TRÁMITE del CRM + verificación) y §7.8 (lista negativa).
> - Datos reales verificados en `movements` (ETAPA=`stage`, TRÁMITE=`procedure`, `description`) y catálogo `hito_tipos`.

---

## 0 · Reglas universales (aplican a TODO hito)

1. **Evidencia PJUD obligatoria.** *"Documento acreditante: captura PJud de la resolución. **Sin captura PJud: sin hito.**"* → el detector adjunta el documento PJUD (`documents.gcs_path`) o no genera el hito.
2. **Devenga sobre el RESULTADO favorable, no sobre la presentación.** §7.8: *"fallo que rechaza la excepción → el abogado actuó pero NO paga. Solo el resultado favorable activa."*
3. **La causa se clasifica por MATRIZ (etapa procesal), no por abogado.** Una misma causa recorre M1 Baja → M1 Alta → M2 → M3. El hito y su valor dependen de la matriz en que ocurre.
4. **Ventana temporal.** El hito cuenta en el mes de su `movement_date` (fecha real en PJUD), no de cuándo se scrapea. Nunca en un período cerrado (`bono_cierre`).
5. **Registro hoy = SYSGAL (manual); verificación = PJUD.** El detector pre-genera en PJUD lo que hoy se carga a mano en SYSGAL.

---

## 1 · Hitos por matriz (Guía V9)

### M1 BAJA — Junior · *causa sin ROL activo / sin notificación*
- **Hito:** Conversión preventiva → activación M1 Alta por escrito.
- **Devengo:** el Estudio **presenta un escrito** ante el tribunal (convierte la causa a M1 Alta o M3). *"La conversión documentada (captura PJud del escrito presentado por el Estudio) genera el hito. Sin captura PJud: sin hito."*
- **Señal PJUD:** un **escrito presentado por el Estudio** en una causa hasta entonces sin actividad. (No requiere resultado favorable — es la presentación misma.)

### M1 ALTA — Pleno/Senior · *causa activada con escrito presentado*
- **Hitos:** Exhibición de documentos, Prescripción, Abandono (6m / 3 años).
- **Devengo:** *"Obtener la resolución favorable — **sentencia firme y ejecutoriada**. Abandono decretado, prescripción acogida, recurso acogido. Documento acreditante: captura PJud de la resolución."*
- **Señal PJUD:** resolución **FIRME + EJECUTORIADA** que acoge/decreta a favor del deudor.

### M2 — Pleno/Senior · *tramitación procesal activa (excepciones · nulidades)*
- **Hitos:** Excepción dilatoria acogida, Incidente de nulidad.
- **Devengo:** *"Obtener la resolución favorable y documentarla — **excepción acogida a tramitación**, nulidad que logre **dilatar el procedimiento ≥ 6 meses**. Captura PJud de la resolución + ROL + fecha. Sin esto: sin hito."*
- **Señal PJUD:** resolución que **acoge** la excepción/nulidad (o que produce la dilación).

### M3 — Senior exclusivo · *ejecución forzada (embargos · remates · apremios)*
- **Hitos:** Tercería acogida, Embargo alzado, Remate suspendido, Acuerdo de pago, cierres.
- **Devengo:** *"Documentar cada gestión con respaldo verificable — captura PJud del escrito presentado, correo con el acreedor, acta de acuerdo firmada, comprobante de pago. Sin documento: sin hito."* Al cerrar: registrar el tipo de cierre (abandono M1 Alta, excepción firme, acuerdo total cobrado).
- **Señal PJUD:** resolución que alza embargo / suspende remate / provee tercería. **Los acuerdos de pago requieren documento firmado + condición del plan (dato SYSGAL, no PJUD).**

---

## 2 · Mapeo técnico hito → ETAPA/TRÁMITE del CRM (V11 §7.6)

| Hito | ETAPA del CRM | TRÁMITE del CRM (resultado) | Verificación |
|---|---|---|---|
| Excepción dilatoria acogida | Ingreso excepciones dilatorias | Sentencia / Fallo favorable | PJud acredita resolución firme |
| Prescripción acogida | Ingreso excepciones prescripción | Fallo favorable al deudor | PJud acredita resolución firme |
| Nulidad acogida | Ingreso incidente nulidad | Fallo favorable al deudor | PJud acredita resolución firme |
| Abandono 6m gestionado | Ingreso abandono 6 meses | Traslado respondido + fallo favorable | Captura PJud el día del traslado |
| Abandono 3 años operado | Ingreso abandono 3 años | Fallo abandono favorable | PJud acredita resolución firme |
| Exhibición resuelta favorable | Exhibición documentos | Fallo / Resolución favorable | PJud acredita resolución |
| Tercería interpuesta y proveída | Embargo muebles / inmueble | Tercería con primera resolución | PJud acredita primera resolución |
| Embargo levantado | Embargo muebles / inmueble | Resolución levanta embargo | PJud acredita resolución |
| Remate suspendido | Embargo inmueble | Suspensión de remate decretada | PJud acredita suspensión |
| Apremio personal evitado | Apremio | Acuerdo de pago suscrito + levantamiento | Documento de acuerdo + resolución PJud |
| Acuerdo de pago antes del remate | Embargo inmueble / muebles | Acuerdo de pago suscrito | Documento firmado + condición del plan |
| Causa cerrada por excepción firme | Finalizado | Proceso terminado por excepción | PJud firmeza + condición del plan |
| Causa cerrada por abandono operado | Finalizado | Proceso terminado por abandono | PJud firmeza + condición del plan |
| Cobro total con acuerdo | Finalizado | Proceso terminado / Conciliación | Documento firmado + condición del plan |

**Ojo (choque de vocabularios):** el CRM usa "INGRESO EXCEPCIONES DILATORIAS"; PJUD scrapeado dice ETAPA "Excepciones"/"Contestación Excepciones" + TRÁMITE "Escrito"/"Resolución". El detector traduce entre ambos.

---

## 3 · El resultado NO está en `description` — está en el PDF

Verificado en datos reales: `movements.description` es una **etiqueta corta del tipo** (promedio 23 caracteres: *"Se pronuncia sobre admisibilidad excep."*, *"Opone excepciones-Traslado"*), **no el resultado**. Los términos de resultado casi no aparecen ahí (`prescri`: 0, `firme`: 0, `rechaz`: 1). Por eso el clasificador es de **dos etapas**:

1. **Candidato (metadata barata):** `(stage, procedure, description-tipo)` → familia de hito.
2. **Resultado (contenido del PDF):** extraer texto del PDF (`pdfplumber` + OCR si es escaneado) y clasificar.

### Keywords para el clasificador (extraídos de la guía)

- **Positivos (favorable):** `acoge` · `acógese` · `ha lugar` · `se acoge` · `decretado` · `declara abandonado` · `prescripción acogida` · `se alza` · `levanta el embargo` · `suspende el remate` · `téngase por exhibido`.
- **Veto (negativo) — descarta el hito:** `no ha lugar` · `se rechaza` · `recházase` · `deniega` · `desestima`. *(Cuidado con la negación: "no ha lugar" contiene "ha lugar".)*
- **Firmeza (obligatoria para M1 Alta y cierres):** `firme` · `ejecutoriada` · `certifíquese ejecutoria`. Si falta, esperar el plazo sin apelación.

---

## 4 · Lista negativa — qué NO activa hito (V11 §7.8)

- **Fallo que RECHAZA** la excepción (actuó pero no prosperó).
- Causa con TRÁMITE *"Sin Defensa de Excepciones"* pasando a embargo (no se defendió).
- Causa cerrada antes del mes 10 del plan sin excepción acogida previa.
- TRÁMITES que son **solo del CRM**, no una presentación en PJUD: *"Redacción Exhibición de Documentos"*, *"Monitoreo e Informe"*, *"Bienvenida Realizada"*.
- Escrito que **no llegó a PJUD** aunque se haya redactado (PJUD es la fuente de verdad).
- **Cambio de TRÁMITE de oficio del tribunal** sin acción del abogado (ej. "Designación de Perito"). → problema de **atribución**.
- TRÁMITE **repetido sin variación** respecto al mes anterior (no hubo gestión).

---

## 5 · Consecuencias para el detector

- **Atribución:** distinguir la acción del abogado de la del tribunal (de oficio) y del acreedor. El M1 Baja/M2 se disparan por un **escrito presentado por el Estudio**; los cambios de oficio no cuentan.
- **Firmeza:** M1 Alta y cierres exigen resolución **firme/ejecutoriada** → detección con retraso (tras la firmeza), aceptable.
- **Condición comercial (SYSGAL):** acuerdos y cierres dependen del **mes del plan del cliente** → no está en PJUD; esos hitos quedan para una fase posterior con integración SYSGAL.
- **Confianza:** positivo fuerte sin veto → alta; ambiguo ("acoge parcialmente", "con costas") → media; sin señal / OCR pobre → baja (sugiere marcado "revisar con cuidado").

Ver el plan técnico del detector para arquitectura, pipeline y fases.
