# Excepciones: el plazo corre desde el requerimiento de pago, no desde la cédula

El motor ancla `excepciones_8d` en la fecha de la `Diligencia:` del movimiento "NOTIFICACIÓN DE DEMANDA"
(`anchor_date`, procedural_classifier.py:60). Carla Segal: el plazo corre desde el REQUERIMIENTO DE PAGO.
Con notificación por cédula (art. 44) el requerimiento es posterior, y el motor adelanta el inicio.

## Specs

- S1: Carla Segal, 2026-10-23, a "¿Entraron un día tarde? ¿O el plazo empezó a correr después de la fecha
  de la diligencia, por ejemplo por una notificación por cédula?": "LA FECHA COMIENZA A CORRER DESDE EL
  REQUERIMIENTO DE PAGO NO DESDE LA ENTREGA DE LA CEDULA". Para `excepciones_8d`, el ancla es la fecha de
  la `Diligencia:` del movimiento "Requerimiento de Pago" (variantes "(Ficto)", "(Personal)",
  "(Realizada)"; NO "(Paga)": el deudor pagó) cuando existe, es >= el ancla de la notificación y no
  está más de 30 días después de ella (el requerimiento normal llega entre 0 y 7 días después; hay
  2 atípicos de 22 y 141 días que no se deben usar para mover un plazo fatal). Se usa el PRIMERO que
  cumple.
- S2: Los movimientos "Requerimiento de pago" SIN `Diligencia:` (439) NO se usan: su fecha es de
  publicación y puede ser posterior al requerimiento real; usarla correría el vencimiento hacia
  adelante de forma insegura. Sin requerimiento válido, el ancla sigue siendo la notificación.
- S3: El ancla nunca es anterior a la de la notificación. No se modela la tabla de emplazamiento
  (no se guarda el lugar del requerimiento); queda documentado como limitación conocida.
- S4: El cambio debe corregir las filas existentes al recalcular, sin duplicar filas
  (`uq_case_deadline_type_triggered`), sin tocar filas manuales ni marcadas por auditor, sin crear
  alertas ni correos nuevos. Efectos medidos en producción: 300 plazos con requerimiento >= ancla,
  ningún plazo `active` cambia de vencimiento hoy, 37 veredictos tardíos pasarían a en plazo, y
  C-11187-2026 (requerimiento ficto 2026-09-21, escrito 2026-09-30) debe salir de la lista de
  `plazo_fatal_incumplido`; las otras 7 marcadas no tienen movimiento de requerimiento y no cambian.

## Tasks

- [ ] T1 · S1-S4 · worker · motor: ancla de `excepciones_8d` en el requerimiento, recálculo de filas existentes, tests
- [ ] T2 · S4 · verificar tras el deploy · recálculo guardado de las causas con plazo `excepciones_8d` afectado (esperado: solo cambian las 300 y, de las 8 marcadas, solo C-11187-2026)

## Log

- L1: (2026-10-23) Carla, 16:0x: "LA FECHA COMIENZA A CORRER DESDE EL REQUERIMIENTO DE PAGO NO DESDE LA
  ENTREGA DE LA CEDULA" (respuesta a la duda sobre C-6216-2026, C-578-2026, C-11312-2026, C-1102-2026,
  C-9790-2026 y C-6751-2026). También respondió: apelación 10 días (hecho en #403), fuera del territorio
  8 días + tabla, abandono 6 meses solo con excepciones opuestas y sin sentencia (3 años si hubo
  sentencia o no hubo defensa), prescripción desde la presentación de la demanda (1 año sin
  notificación), no existe el feriado judicial.
- L2: Medido: formas del movimiento de requerimiento con `Diligencia:`: Ficto 865, Personal 168,
  Realizada 8, Paga 5; sin `Diligencia:` 439. Etapas: Exhorto 772, Mandamiento 612, Requerimiento pago 71.
  Diferencia requerimiento - ancla (días): 0:109, 1:116, 2:22, 3:28, 4:11, 5:6, 6:4, 7:2, 22:1, 141:1.
- L3: Código: `anchor_date()` (procedural_classifier.py:60) y su uso en :216; el disparador se define en
  deadlines_config.py (`NOTIFICACION_EXITOSA`, ~:303-312); `_current_due_date(row)`
  (deadline_engine.py:1050) re-deriva el vencimiento desde `row.source_movement`;
  `_triggered_at_date` (:1317). La fila se identifica por (case_id, deadline_type, triggered_at).
