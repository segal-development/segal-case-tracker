# Propuesta de Infraestructura — Automatización del Monitoreo de Causas (PJUD)

**Dirigido a:** Gerencia
**Preparado por:** Equipo de Desarrollo
**Fecha:** Junio 2026 · **Actualizado:** Julio 2026 (con datos de rendimiento medidos en producción)
**Decisión requerida:** Destinar un equipo dedicado, encendido de forma permanente en la oficina, para dejar el monitoreo automático. La opción preferente es **reutilizar un equipo que el estudio ya posea (inversión US$0)**; si no hubiera uno disponible, un mini-PC nuevo (~US$150–450).

---

## 1. Resumen ejecutivo

El sistema que monitorea automáticamente las causas del estudio en el Poder Judicial (PJUD) —movimientos, plazos, semáforo de alertas y documentos— **ya está funcionando**. Sin embargo, hoy depende de que un computador personal esté encendido y atendido manualmente, lo cual **no es sostenible**.

Para que el monitoreo quede **automático, permanente y sin depender de ninguna persona en particular**, recomendamos destinar **un equipo dedicado** (un mini-computador de bajo costo) que funcione de forma continua en la oficina, como una pieza más de infraestructura (al igual que el router de internet).

La alternativa "en la nube" **no resuelve el problema** por una restricción técnica del propio PJUD (se explica en la sección 3) y además implica un **costo mensual permanente** sin garantía de funcionamiento.

---

## 2. Contexto: qué hace el sistema y por qué importa

El sistema revisa diariamente el portal del Poder Judicial y mantiene actualizada, para cada causa del estudio:

- Los **movimientos nuevos** (resoluciones, notificaciones, trámites).
- Los **plazos procesales** y un **semáforo de alertas** (rojo/amarillo/verde) para actuar antes de vencimientos fatales.
- Los **documentos** del expediente.
- Una vista de **novedades** por abogado (qué cambió desde la última vez que ingresó).

**Valor para el estudio:** reduce el riesgo de perder plazos, ahorra horas de revisión manual del portal y da visibilidad inmediata del estado de la cartera. Para que ese valor sea real, **la información debe estar siempre fresca, sin intervención manual.**

---

## 3. El desafío técnico (en términos simples)

El portal del Poder Judicial tiene un **sistema de defensa contra automatización** (tecnología "anti-bot" de la empresa F5). Verificamos en la práctica que este sistema **bloquea** la conexión cuando detecta:

1. Que el acceso proviene de un **servidor en la nube** (las direcciones de internet de proveedores como Google/Amazon están marcadas y bloqueadas), y
2. Que se usa un **navegador automatizado "oculto"**.

En cambio, **sí permite el acceso** cuando se cumplen ambas condiciones:

- Se usa una **conexión de internet común** (residencial / de oficina, como la del estudio), y
- Se usa un **navegador real** (como el que usa cualquier persona).

**Conclusión:** el monitoreo automático requiere, sí o sí, **un equipo conectado a la red de la oficina, encendido de forma continua.** No es una elección de comodidad; es la única forma técnica de que el portal no bloquee el acceso.

---

## 4. Situación actual

Hoy el monitoreo corre sobre el **computador personal de un integrante del equipo**. Esto tiene tres problemas:

- **No puede estar siempre encendido** (se apaga al final del día, fines de semana).
- **Depende de una persona** que lo inicie y supervise.
- **Interrumpe el trabajo** de esa persona mientras corre (abre ventanas del navegador).

Si esa persona no está disponible, **el monitoreo se detiene** y la información queda desactualizada.

---

## 5. Opciones evaluadas

| Criterio | **Opción A — Equipo dedicado en la oficina** *(recomendada)* | **Opción B — Servidor en la nube + proxy** |
|---|---|---|
| **Cómo funciona** | Un mini-computador conectado a la red de la oficina, encendido siempre, dedicado solo a esta tarea | Un servidor en la nube que simula una conexión residencial mediante un servicio de "proxy" pago |
| **Costo** | **Inversión única ~US$150–450** (mini-PC nuevo o Mac usado). Consumo eléctrico: centavos al mes | **Costo mensual permanente ~US$15–40/mes** (servidor en la nube + 1 dirección residencial fija) |
| **¿Funciona con seguridad?** | **Sí — verificado.** Es la misma configuración que ya usamos | **Incierto — a validar.** La investigación técnica concluyó que la dirección de internet del proveedor puede ser igualmente detectada y bloqueada por el sistema del PJUD. Es la única incógnita; se resuelve con una prueba de bajo costo (ver 5.1) |
| **Dependencia** | Solo que el equipo siga enchufado (como el router) | Que alguien pague la mensualidad y gestione bloqueos del proxy |
| **Continuidad si cambia el personal** | **Alta** — ya comprado, funciona solo | Media — requiere gestión y pago continuo |

> Nota: una "máquina virtual" en la nube **no es una alternativa distinta**: tiene el mismo problema de dirección de internet de servidor descrito en la sección 3. Solo funcionaría sumándole una dirección residencial fija (proxy), lo que nos lleva a la Opción B.

### 5.1 Validación del camino en la nube (prueba de bajo costo, opcional)

La Opción B tiene **una sola incógnita**: si la dirección residencial fija contratada
logra pasar el sistema de defensa del PJUD de forma estable. **No se puede afirmar sin
probarlo** (los proveedores lo aseguran en su publicidad, pero la investigación técnica
encontró que no está garantizado para este sistema en particular).

Antes de descartar la nube, se puede ejecutar una **prueba acotada (~US$10–30, una sola
vez, cancelable)** que confirma o descarta la Opción B con datos reales en 5–7 días:

- **Si la prueba pasa** → el monitoreo puede vivir 100% en la nube, sin equipo físico
  (~US$15–40/mes), lo que da continuidad total sin depender de hardware en la oficina.
- **Si la prueba falla** → se confirma la Opción A (equipo dedicado), ahora con evidencia.

Esta prueba es la forma responsable de decidir entre "comprar equipo" y "pagar mensualidad",
sin apostar a ciegas en ninguna dirección. *(Detalle técnico en el documento de respaldo.)*

### 5.2 Rendimiento: por qué la velocidad dejó de ser un problema *(medición Julio 2026)*

Al poner el sistema a cubrir la cartera completa, encontramos y resolvimos el factor que más
lo enlentecía:

- **De dónde venía la lentitud:** la base de datos del sistema vive en un servidor en el
  exterior (EE.UU.), y cada consulta viaja ida y vuelta desde el equipo que monitorea. Con
  muchas consultas por causa, eso sumaba alrededor de **30 segundos por causa**.
- **Qué hicimos:** reducimos drásticamente la cantidad de consultas por causa. La mejora,
  **medida en producción, fue de ~2,7× en velocidad** (de ~2 a ~5,5 causas por minuto), sin
  ningún cambio de infraestructura.
- **La clave operativa:** con un equipo **encendido las 24 horas**, la velocidad deja de
  importar. El equipo trabaja solo —de noche y los fines de semana— y va cubriendo la cartera
  sin supervisión. Como referencia real medida: la cartera de un abogado con ~4.400 causas se
  cubre en **menos de un día de trabajo continuo** del equipo; algo imposible en un computador
  personal que se apaga y hay que atender.

Esto **refuerza la Opción A**: el equipo dedicado no solo elimina la dependencia de una persona
—también convierte la velocidad en un tema resuelto, por el simple hecho de estar siempre encendido.

---

## 6. Recomendación

**Destinar un equipo dedicado (Opción A)**, operado como infraestructura permanente de la oficina.
La forma preferente es **reutilizar un equipo que el estudio ya posea (inversión US$0)**; solo si
no hubiera uno disponible, adquirir un mini-PC nuevo (~US$150–450).

- **Económicamente:** una inversión única menor frente a un costo mensual indefinido.
- **Técnicamente:** es la configuración **ya probada y funcionando**, sin incertidumbre.
- **Operativamente:** queda **automático**, sin depender de ninguna persona.

Opción preferente de menor costo: **reutilizar un equipo que el estudio ya posea** (cualquier computador que pueda quedar encendido de forma permanente en la oficina), lo que reduce la inversión a **US$0**.

> Si Gerencia prioriza **no tener ningún equipo físico**, recomendamos ejecutar primero la **prueba de bajo costo de la sección 5.1** antes de decidir: confirma o descarta la nube con datos, por ~US$10–30 una sola vez.

---

## 7. Cómo garantizamos que funcione "solo" (autonomía)

Para que el sistema sea verdaderamente automático y **sobreviva a cambios de personal**, se incorpora:

1. **Arranque automático:** si se corta la luz y vuelve, el monitoreo se reinicia solo.
2. **Auto-recuperación:** si el proceso falla, se reinicia automáticamente.
3. **Alerta por correo:** si el monitoreo deja de funcionar (por ejemplo, no logra actualizar en 24 horas), el sistema **envía un aviso automático** para que el estudio lo sepa de inmediato.

Con esto, la única atención humana necesaria es **mantener el equipo enchufado** —lo mismo que ya ocurre con el router o el servidor de internet.

---

## 8. Consideración legal (a evaluar por el estudio)

El monitoreo accede al portal del Poder Judicial **usando las credenciales institucionales (Clave Única) del estudio**, de forma automatizada. Recomendamos que **Gerencia y el área legal revisen** los Términos de Uso del portal y la normativa aplicable, para confirmar que esta automatización se ajusta a las políticas vigentes. Es una verificación de buenas prácticas, independiente de la viabilidad técnica.

---

## 9. Qué necesitamos de Gerencia

1. **Aprobación** de la Opción A (equipo dedicado) y, si corresponde, del presupuesto de inversión única (~US$150–450), o la indicación de **reutilizar un equipo existente**.
2. **Confirmación** de un espacio en la oficina donde el equipo quede encendido de forma permanente.
3. **Visto bueno** del área legal sobre el punto 8.

Una vez aprobado, el equipo de desarrollo deja la solución instalada y operativa, sin intervención adicional del estudio.

---

*Documento de respaldo técnico disponible para consultas.*
