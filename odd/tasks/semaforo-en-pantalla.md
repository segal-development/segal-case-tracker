# El semáforo de plazos fatales, visible en el inicio

## Objetivo

Que al abrir la aplicación se vea, con colores y sin buscar, qué plazos fatales
están por vencer y cuáles ya se vencieron sin presentar.

## El problema

El semáforo de excepciones existe y funciona: evalúa, asigna nivel, crea alertas
y manda correos. Pero **no se ve en ninguna pantalla**. Su nivel no lo expone
ningún endpoint, y en la aplicación solo llega como texto dentro de una alerta,
con campana gris, indistinguible de una notificación cualquiera.

Un correo se pierde. Una pantalla no. El estudio pidió verlo "de manera clara y
con los colores" en el inicio.

**OJO, no confundir dos semáforos distintos:**

| | Qué mide | ¿Se ve hoy? |
|---|---|---|
| `cases.semaforo` (el viejo) | Todos los plazos accionables, umbral rojo ≤1 / amarillo 2-5 días hábiles | Sí: anillo en Plazos, Causas, Detalle y Dashboard |
| `deadline_semaforo.assess` (el nuevo) | Las escalas del flujo de excepciones dilatorias | **No, en ningún lado** |

Este trabajo hace visible el SEGUNDO. **El primero no se toca.**

## Alcance

Los tres plazos **fatales** (`is_fatal=True` en `deadlines_config`), porque
perderlos mata la causa:

| Tipo | Vivos hoy | Vencidos sin presentar (30 d) |
|---|---|---|
| `excepciones_8d` | 4 | 23 |
| `acreditar_poder_3d` | 4 | 11 |
| `apelacion_5d` | 2 | 0 |
| **total** | **10** | **34** |

### El panel muestra las dos caras

Mostrar solo lo vivo da 10 filas y la pantalla parece rota. Y los 34 vencidos
—que son el dolor real— seguirían invisibles. Que haya pocos vivos no es un
fallo del semáforo: es el síntoma de la detección lenta (si nos enteramos tarde,
el plazo ya no está vivo cuando lo vemos).

```
POR VENCER        rojo / amarillo / verde segun assess()
YA VENCIDOS       sin presentar, ultimos 30 dias
```

Cada fila: color, ROL, carátula, tribunal, abogado, tipo de plazo, fecha de
vencimiento y días restantes. Clic lleva a la causa.

## Restricciones (si se violan, se rechaza)

1. **El color sale de `deadline_semaforo.assess`**, nunca de una regla nueva ni
   del semáforo viejo. Si `assess` devuelve `level=None`, la fila no se pinta.
2. **`cases.semaforo` y el componente `SemaforoRing` no se tocan.** El anillo
   que ya existe en Plazos, Causas, Detalle y Dashboard sigue igual.
3. **Se respeta `_is_ours`**: un plazo de la contraparte no se muestra. Mandar a
   un abogado a perseguir el plazo de otro es peor que no mostrar nada.
4. **El plazo sin confirmar se muestra como tal.** Para excepciones el término
   es de 4 u 8 días hábiles y no sabemos cuál: el panel muestra el nivel más
   urgente de las lecturas vivas y deja ver las dos fechas. Nunca afirma una.
5. **La ventana de vencidos es configurable por variable de entorno**, no una
   constante.
6. **Solo lectura.** El panel no crea alertas, no manda correos y no escribe
   nada: esos caminos ya existen y son de `semaforo_alerts`.
7. **Copy en español neutro con tuteo**, sin voseo.

## Modo TDD

Habilitado. Runner backend: el venv de Poetry
(`.../segal-case-tracker-VujtO8MO-py3.11/bin/python -m pytest`).
Más `mypy app/core`, paso del CI que ya costó un deploy fallido.
Frontend (`/Users/marcelo/Projects/segal-deudores-web`, rama por defecto
`master`): vitest, y `npm run build` — nunca `vite build` directo.

## Tareas

- [x] **T1** (3daed52) — Backend: endpoint de solo lectura que devuelve, por plazo fatal,
      el nivel de `assess` y los datos de la causa. Vivos y vencidos recientes.
- [x] **T1b** (9f6793b) — Escala de `apelacion_5d` en `assess` (verde 1-2, amarillo 3-4, rojo 5; término único, confirmado).
- [ ] **T2** — Verificación contra la base real: contar lo que devuelve el
      endpoint y compararlo con 10 vivos / 34 vencidos.
- [ ] **T3** — Frontend: el panel en el inicio, con colores.
- [ ] **T4** — Que las alertas `plazo_rojo`, `plazo_amarillo` y `plazo_vencido`
      dejen de salir con campana gris en Novedades.

## Progreso

Documento creado 2026-10-19 tras medir la población. Decisión acordada con
Marcelo: los tres plazos fatales, no solo excepciones; vivos y vencidos juntos.

- T1 (3daed52): `GET /api/v1/cases/deadlines/semaforo`, servicio `app/services/semaforo_panel.py`.
- T2 pendiente de ticket: script `scripts/check_semaforo_panel.py` listo (commit "chore(plazos): script para contar el panel...");
  falta correrlo contra la base real. Diferencias esperadas con 10/34: `apelacion_5d`
  ya tiene semáforo (T1b), así que entra en por_vencer; y `acreditar_poder_3d`
  vencido no es provablemente nuestro (queda fuera de vencidos).
