# Diagnóstico de causa, en pantalla

## Objetivo

Que cualquier admin pueda escribir un ROL o un RUT y saber en el acto por qué el
sistema no muestra lo que el cliente avisa, sin depender de nadie.

## El problema

Los clientes avisan movimientos que el sistema no tiene. Buscar la causa y no
encontrarla **no dice nada**: puede faltar, estar bajo un abogado sin
credencial, o estar y no revisarse hace semanas. Tres problemas distintos con
tres arreglos distintos.

Hoy eso se responde con un script que corre el desarrollador
(`scripts/que_paso_con_la_causa.py`, PR #364). Eso no sirve cuando la llamada
entra un sábado.

## Los cuatro veredictos

| Veredicto | Qué significa | Qué se hace |
|---|---|---|
| **No está en la base** | Nunca la ingerimos | Agujero de cobertura; ninguna mejora de velocidad la trae |
| **Abogado sin credencial o inactivo** | La causa existe pero no se revisa | Cargar la credencial o reasignar |
| **Latencia** | Se revisó hace más de 7 días | El movimiento puede estar en el PJUD y todavía no acá |
| **Al día** | Revisada hace poco | Único caso que obliga a mirar el PJUD a mano |

Además lista **todas las filas que comparten ese ROL**, con su tribunal y su
abogado: ahí se ve el bug de identidad de un vistazo (en la prueba,
`C-13510-2024` devolvió tres filas, dos de ellas bajo abogados distintos en el
mismo tribunal).

## Alcance

- **Solo para roles transversales** (`admin`, `auditor`). Expone si un abogado
  tiene credencial cargada, que no es dato para todos. Nunca expone la
  credencial en sí, solo si existe.
- **Solo lectura.** No consulta el PJUD, no abre sesiones, no escribe nada.
- Busca por ROL exacto o por RUT del demandado.

## El contrato del endpoint (congelado, para que front y back vayan en paralelo)

`GET /api/v1/cases/diagnostico?rol=<ROL>` o `?rut=<RUT>`

```
{
  buscado: string,
  encontrado: boolean,
  filas: [{
    case_id, rol, caratula, tribunal|null, estado,
    abogado_id|null, abogado_nombre|null,
    abogado_activo: bool|null,
    abogado_tiene_credencial: bool|null,
    last_detail_checked_at: string|null,
    dias_desde_revision: number|null,
    last_movement_at: string|null,
    movimientos: number,
    ultimo_movimiento: { fecha, descripcion }|null,
    veredicto: "sin_credencial"|"abogado_inactivo"|"nunca_revisada"|"latencia"|"al_dia",
    veredicto_detalle: string      // en espanol neutro, listo para mostrar
  }]
}
```

Con `encontrado: false` y `filas: []` cuando no hay ninguna.

## Restricciones (si se violan, se rechaza)

1. **Nunca devolver la credencial**, ni cifrada, ni su hash, ni un fragmento.
   Solo el booleano de si existe. Es la misma regla de `/credentials`.
2. **Solo lectura**: no toca el PJUD y no escribe en la base.
3. **El umbral de latencia es variable de entorno**, no una constante.
4. Reusa el control de acceso de los endpoints transversales que ya existen;
   no inventa uno nuevo.
5. Copy en español neutro con tuteo, sin voseo.

## Lo que esto NO resuelve

Es un termómetro, no la cura. El veredicto "no está en la base" seguirá
apareciendo hasta que cerremos el agujero de ingesta (~12 % de la cartera, ver
`agujero-de-ingesta-causas-faltantes` en memoria). El paso siguiente natural
—traer la causa del PJUD en el momento— exige una sesión y hoy obliga a pausar
la estación (`consulta_sync.py`: "one PJUD session per IP at a time"). Queda
fuera de esta rebanada.

## Modo TDD

Habilitado. Runner backend: el venv de Poetry. Más `mypy app/core`.
Frontend (`segal-deudores-web`, rama por defecto `master`): vitest y
`npm run build`, nunca `vite build`.

## Tareas

- [ ] **T1** — Backend: el endpoint con los cuatro veredictos.
- [ ] **T2** — Frontend: la pantalla donde se escribe el ROL o el RUT.
