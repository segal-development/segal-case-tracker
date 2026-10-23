# Abandono: 6 meses solo con excepciones opuestas; 3 años si hubo sentencia o no hubo defensa

El motor (`_compute_abandono`, deadline_engine.py ~:107) aplica 6 meses a todo estado anterior a la
sentencia y 3 años solo a SENTENCIA/REBELDE. REBELDE solo se asigna al lado demandante, así que el
ejecutado que NO opuso excepciones queda en NOTIFICADO y recibe 6 meses: recomendaciones prematuras
de "solicitar abandono".

## Specs

- S1: Carla Segal, 2026-10-23, a "Abandono... el sistema recomienda 'solicitar abandono' tras 6
  meses sin movimiento... Pero si el ejecutado no opuso excepciones y el mandamiento hace de
  sentencia, el art. 153 habla de 3 años. ¿Aplica ese caso a lo que ustedes ven?": "HAY DOS TIPOS DE
  ABANDONOS EL DE 6 MESES PROCEDE CUANDO HAY EXCEPCIONES OPUESTAS Y POSTERIOR A ESO NO HAY GESTIONES
  UTILES EN EL PROCEDIMIENTO Y NO HAY SENTENCIA AÚN RESPECTO DE ESTA DEFENSA PRESENTADA. AHORA BIEN SI
  YA SE DICTO SENTENCIA RESPECTO A LAS EXCEPCIONES O NO SE PRESENTO DEFENSA EL PLAZO PARA SOLICITAR
  ABANDONO SE AMPLIA A 3 AÑOS DESDE LA ÚLTIMA GESTIÓN ÚTIL DEL EJECUTANTE".
- S2: Regla: 6 meses SOLO si hay excepciones opuestas y todavía no hay sentencia sobre ellas (estados
  EXCEPCIONES, TRASLADO_EJECUTANTE, ADMISIBILIDAD, AUTO_PRUEBA, CITACION_SENTENCIA). 3 años si ya hay
  sentencia (SENTENCIA) o si NO se presentó defensa: el ejecutado notificado que no opuso excepciones
  (estado NOTIFICADO una vez notificado el requerimiento). El estado previo a la notificación
  (MANDAMIENTO) se mantiene en la regla general de 6 meses (art. 152): Carla no lo cubrió; documentarlo.
- S3: "Última gestión útil del ejecutante": hoy el motor cuenta desde el último movimiento de cualquier
  tipo. No hay forma fiable de distinguir una gestión útil: no se cambia y queda como limitación
  documentada (es conservadora en el sentido de que adelanta la disponibilidad).
- S4: Efecto esperado medido en la base: `abandono_disponible` = 613 causas (por estado: excepciones 314,
  notificado 221, traslado_ejecutante 111, auto_prueba 35, mandamiento 28, citacion_sentencia 23,
  admisibilidad 19, sentencia 7). Con la regla nueva deberían desaparecer la mayoría de las 221 de
  NOTIFICADO (si pasan de 6 meses pero no de 3 años) y el resto no cambia.

## Tasks

- [ ] T1 · S1-S4 · worker · motor: regla de abandono por defensa, textos de la recomendación, tests
- [ ] T2 · S4 · verificar tras el deploy · recálculo guardado de las causas con `abandono_disponible` y medición del antes/después

## Log

- L1: (2026-10-23) Carla, 15:53: "3- HAY DOS TIPOS DE ABANDONOS ..." (texto completo en S1).
- L2: Marcelo: "Si" a continuar con la regla del abandono.
