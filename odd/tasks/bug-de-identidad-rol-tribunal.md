# Bug de identidad: el mismo ROL en cortes distintas

**Estado:** mapeado, NO urgente. Ver "Prioridad" antes de empezar.

## El problema, corregido

No son "causas invisibles". El upsert **no choca** contra la clave única
`uq_cases_lawyer_rol`: precarga un diccionario por ROL y pisa la fila.

```python
existing = existing_by_rol.get(rol)   # solo ROL, sin tribunal
existing.court_id = court.id          # sobreescribe el tribunal
```

`sync_service.py:1255-1281`. Si el listado trae `C-13412-2026` en el 15º y en
el 25º, la primera crea la causa y la segunda **la sobreescribe**. Sin error,
sin log. También pisa `plaintiff`, `defendant`, `status` y `procedure`. Las dos
causas colapsan en una fila que mezcla datos de ambas.

## Prioridad: BAJA, y acá está el porqué

Medido 2026-10-19:

```
                           afectadas    total     peso
causas con ROL compartido     2.186     15.142     14 %
en etapa de notificacion        574      3.378     17 %
con plazo de excepciones vivo    11         62     18 %
hoy en rojo                      28        187     15 %
```

**Once plazos vivos de 62.** Arreglarlo perfecto no mueve la aguja del objetivo
real ("que no se escape ninguna excepción"), porque el 97 % de los plazos vivos
ya venció — y vencen por los 17 días de demora, no por este bug.

**Primero el delta-sync y el semáforo con alertas. Después esto.**

## La excepción que SÍ hay que hacer antes

`document_identity_hash(doc_type, case_rol, scope_key)`
(`document_persistence.py:34-52`) **no incluye el tribunal**, y el índice es
único **global**:

```sql
CREATE UNIQUE INDEX ix_documents_pjud_token_hash ON documents (pjud_token_hash)
```

En cuanto aparezcan las causas faltantes, dos del mismo ROL generan el mismo
hash, el INSERT falla y **se revierte toda la fase de documentos de esa causa**.
Arreglar la clave sin esto rompe la descarga de documentos en silencio.

## El orden, cuando llegue el turno

1. El hash de documentos incluye el tribunal. **Sin esto no se puede seguir.**
2. El upsert deduplica por `(abogado, ROL, tribunal)` — `sync_service.py:1255`.
3. La clave única migra a `(lawyer_id, rol, court_id)` — modelo
   `case.py:58`, creada en `021_ingest_keys_and_unique_rol.py:23`. Sin riesgo de
   fallar por datos: la clave nueva es un superconjunto, y `court_id` es
   `NOT NULL` desde la 001.
4. Los lookups críticos por ROL.

## Los lookups que devolverían la causa equivocada

- **`sync_service.py:2528`** — elige la causa del detalle con `.first()` por
  `(lawyer_id, rol)`, sin tribunal, teniéndolo disponible en `api_case`. **De
  ahí salen los movimientos, los plazos y las alertas.** Es el peor.
- **`ingest_service.py:482, 601, 697`** — el protocolo de la extensión
  (`pjud_ingest.py:48-58`) **ni siquiera manda el tribunal**: arreglarlo exige
  cambiar también la extensión.
- `client_sync.py:111` — etiquetaría con el cliente a la causa homónima.
- `sync_service.py:1728` — `api_lookup` por ROL colapsa dos entradas del listado.

Toleran dos resultados sin romper: las búsquedas `ilike` de `cases.py:613`,
`redaccion/buscar.py:99` y `renovaciones.py:362`.

El motor de plazos, el de alertas y el detector de hitos van por `case_id`: no
se tocan.

## Hay daño pasado, no solo riesgo

La migración 024 (`case_merge`) agrupó por ROL **sin mirar el tribunal** y borró
las perdedoras:

```
fusiones registradas en case_merge_audit     229
de esos ROL, hoy existen en varias cortes     96
los 229 ganadores siguen existiendo
```

`case_merge_audit` guarda ROL e ids pero **no el tribunal**, así que no se puede
reconstruir desde ahí qué se fusionó indebidamente. **Decisión para el estudio,
no técnica.**

## Cuatro caminos crean causas, no uno

| Camino | Dónde | Deduplica por |
|---|---|---|
| `SyncService._upsert_case` | `sync_service.py:1284` | `(lawyer_id, rol)` |
| Ingest de la extensión | `ingest_service.py:186, 200` | ROL, con `firm_lawyer_id` |
| `scripts/import_cases_html.py:120` | script | solo ROL |
| `case_merge.reverse_case_merge` | `case_merge.py:196` | usa el court del ganador |

## Trampas conocidas

- **Tribunal vacío**: `sync_service` acepta `""` y crearía un `Court` con nombre
  vacío; el ingest usa "Desconocido". Todas las causas sin tribunal caerían en
  la misma corte y **volverían a chocar entre sí** con la clave nueva.
- **`Court.name` no es único** (solo `code`). `_get_or_create_court` hace
  `.first()` sin orden: dos grafías del mismo juzgado = dos `Court` distintos.
- **El docstring de `Case` (ADR-007) está desactualizado**: dice que todas las
  filas comparten el `lawyer_id` de la firma. Verificado contra la base el
  2026-10-15: hay **14 `lawyer_id` distintos**. Eso solo es cierto en el ingest
  por extensión.
- **Sysgal expone `rol` sin `case_id`**: con dos causas homónimas, un consumidor
  que use el ROL como clave se confunde. `redaccion/buscar.py` sí devuelve
  `case_id`.

## Lo que no se verificó

- Si la 024 corrió en producción y qué fusionó allí.
- Duplicados de `Court.name` en la base.
- Si la extensión puede mandar el tribunal de vuelta.
