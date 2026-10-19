# Semáforo de plazos con escalamiento

Implementa los dos semáforos de la especificación "Flujo Excepciones
Dilatorias" (Dirección Jurídica, 2026-10-14), con sus niveles y destinatarios.

## La escala, del documento

**Semáforo 1 · excepciones**

| Plazo | Verde | Amarillo | Rojo |
|---|---|---|---|
| 8 días (fuera de la comuna, dentro del territorio) | días 1-5 | 6 y 7 | día 8 |
| 4 días (comuna asiento del tribunal) | días 1 y 2 | día 3 | día 4 |

**Semáforo 2 · ratificación (3 días)**: verde día 1, amarillo día 2, rojo día 3.

**Destinatarios**, confirmados por Marcelo el 2026-10-14. La estructura es
PLANA, no un mapeo por abogado:

```
verde     →  abogado
amarillo  →  abogado + Melissa Acevedo   (lawyer id 15)
rojo      →  abogado + Melissa + Carla   (Carla Lavín, lawyer id 1)
```

## Volumen, medido

```
excepciones_8d       94,4 plazos nuevos por día hábil
acreditar_poder_3d   59,3
                    ─────
                    ~154 por día  =  11 por abogado por día
```

**El verde se envía agrupado**: un correo por abogado por día con sus plazos
nuevos. Marcelo confirmó que el verde es una alarma de verdad y tiene que
llegar; agruparlo hace que llegue sin volver a once correos sueltos, que es
exactamente el problema que los PRs #336 a #338 acaban de resolver.

**Amarillo y rojo van inmediatos y uno por uno.** Ahí el ruido no importa.

## El plazo que no sabemos, y qué hacer con eso

`lugar_requerimiento` **no existe en ninguna fuente**: no está en el PJUD
(1.195 movimientos de requerimiento, 0 mencionan comuna o domicilio), no está
en las notificaciones (18.251 filas, 0), no hay columna en la base, y Sysgal
solo expone estado comercial. Vive en el estampado del receptor, dentro de
PDFs que sí tenemos (478 guardados). Marcelo lo está preguntando al estudio.

Hoy el código fija 8 días para todos, citando el art. 459 — que según el
documento es el del plazo de 4 (el de 8 es el art. 460).

**Entonces: donde no sabemos el plazo, el sistema lo dice.** No se asume 8.
Asumir 8 en una causa de 4 es mandar la alarma roja cuatro días tarde, sobre
un plazo fatal. Es preferible una causa marcada "plazo sin confirmar" que una
alarma verde que miente.

## Restricciones (si se violan, se rechaza)

1. **No se rompe lo de notificaciones** (#336, #337, #338): el resumen por
   abogado, los webhooks inmediatos, las accionables inmediatas y la verdad
   de `email_sent` quedan intactos.
2. **Amarillo y rojo son accionables**: inmediatos, nunca dentro del resumen.
3. **El verde agrupado no puede perderse**: si el envío falla, las alertas
   quedan sin marcar, igual que el resumen de movimientos.
4. **Un fallo del semáforo nunca rompe el ciclo de sync.**
5. **Nunca una alarma que afirme un plazo que no conocemos.** Si el plazo de
   excepciones no está confirmado, el aviso lo declara.
6. **Línea de corte.** No se genera alerta (ni correo ni webhook) para un plazo
   cuyo `triggered_at` sea anterior a `hoy - SEMAFORO_MAX_TRIGGER_AGE_DAYS`
   días. Lo viejo sigue visible en la pantalla de Plazos. Aplica a los tres
   colores. `triggered_at` NULL no alerta (se falla hacia el silencio). Valor
   0 o negativo = sin corte. Se registra en el log (`semaforo: N plazo(s)
   suppressed by the cutoff`) y en el resumen (`suppressed_by_cutoff`).
   Default 14, no 7: el rojo de un plazo de 8 días hábiles cae 9 a 12 días
   corridos después del disparo, así que un corte de 7 silenciaría justo el
   escalamiento.

## Por qué existe el corte (medido antes de encender)

```
plazos ACTIVOS que alertarian        1.595
   de esos, YA VENCIDOS              1.580   (99 %)
   accionables (hoy o manana)            7
   verdes de verdad                      8

antiguedad de los vencidos:
   hace 2-19 dias      124
   hace 20-59 dias     210
   hace 60-119 dias    320
   hace MAS de 120     926      (el mas viejo: 1.969 dias)
```

Sin corte, el primer ciclo mandaría 1.580 alarmas rojas sobre plazos muertos.

## Fuera de alcance

- La **toma de control** del día 3 (hora de corte 12:00, reasignación a la
  supervisora) y el campo `cumplido_por`.
- El **checklist de 7 ítems**: cuatro de los siete requieren que un humano lea
  documentos.
- **Parsear el PDF del receptor** para sacar el lugar del requerimiento.

## Modo TDD

Habilitado. Runner: el venv de Poetry.

## Tareas

- [ ] **T1** — Niveles del semáforo + destinatarios + el verde agrupado.

## Progreso

(sin iniciar)
