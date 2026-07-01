# Changelog — Sebastian

Registro de cambios para no pisar trabajo previo. Lo más nuevo arriba.

## 2026-07-01

- **bot.py — "mostrame los gastos" prometía la lista pero no la mandaba.**
  El modelo respondía "al final verá la lista detallada..." pero nunca se mostraba
  (para gastos NO había render determinístico como en tareas; el modelo debía escribirla
  y a veces no lo hacía, contagiado de la regla "no escribas la lista de tareas"). Fix:
  render determinístico `_format_gastos_lista` (resumen por categoría + detalle + total),
  rama `get_expenses` en `_run_tool_calls` que lo pone como direct_reply, y prompt
  ajustado ("la lista la muestra el sistema, no la escribas vos").

- **bot.py — "mostrame la lista del súper" mostraba las TAREAS.**
  En `_route_text`, el handler de lista de tareas (`_is_task_list_request`) corre antes
  que el del súper, y "la lista del super" contiene "la lista" → ganaba tareas y hacía
  return. Fix: `_is_task_list_request` ahora devuelve False si el texto menciona
  super/súper/compras/listado (mismo patrón que la exclusión de gasto/fijo que ya había).

- **bot.py — REVERTIDO el guardrail de recurrencia por aliases (era el enfoque equivocado).**
  Se probó empíricamente contra gpt-4.1 (scratchpad/exp_recurrencia*.py): con el prompt
  actual el modelo YA interpreta bien recurrencias, incluso frases novedosas que ninguna
  lista cubriría ("cada mañanita de acá a fin de año" → diario hasta 31/12; "tres veces
  por día durante agosto" → 3 eventos diarios; "el primero de cada mes" → gasto fijo).
  Por eso se eliminaron `_recurrence_from_text` / `_hasta_from_text` (parche frágil por
  frases). Lo que resuelve el caso es el PROMPT que enseña el CONCEPTO (repetir/hasta +
  1 ejemplo trabajado), que generaliza. Se conserva el ejemplo en el prompt.
  Nota estratégica: la arquitectura de `_is_*_intent` + `tool_choice` forzado + toolset
  recortado es deuda del modelo viejo (gpt-4o-mini); gpt-4.1 anda bien con auto+todas las
  tools. Candidato a simplificar más adelante (no urgente, no rompe nada hoy).

- **bot.py — Borrado de evento con match FLEXIBLE por palabras clave.**
  El usuario parafrasea el título ("borrame el cable 19 por 2,5" para "Averiguar
  precio de mangueras de 19 por 2,5 milímetros con cable Sur") y el borrado, que
  matcheaba por substring exacto, decía "no existe". Nuevos helpers
  `_norm_event_tokens` / `_event_match_score` / `_best_event_matches` (sin acentos,
  sin muletillas, score = fracción de palabras clave del pedido presentes en el
  título, umbral 0.5). El flujo de `delete_event` usa match flexible y, si la
  búsqueda `q` de Google no engancha, cae a `get_upcoming_events` (nueva, ventana
  -1..+120 días) y matchea ahí.

- **bot.py — Anti-duplicado de create_event.**
  Con `tool_choice="required"` (para permitir varios eventos en un mensaje) el modelo
  a veces repetía la MISMA llamada create_event y se creaba el evento dos veces. Se
  descartan create_event con firma idéntica (nombre+fecha+hora+hora_fin+repetir+hasta)
  dentro de la misma tanda.

- **google_services.py — Borrar una serie recurrente borra TODA la serie.**
  Con `singleEvents=True` el borrado recibía el id de una INSTANCIA y Google sacaba
  solo ese día. Ahora `delete_event` hace un `get` del evento: si tiene
  `recurringEventId` (es instancia de una serie), borra el evento MAESTRO (toda la
  serie). Trade-off: por ahora no se puede borrar una sola ocurrencia de una serie
  (se borra entera), que es lo que el usuario espera al decir "eliminá esos eventos".

- **bot.py — Hotfix: `'ChatCompletionMessage' object has no attribute 'get'`.**
  El detector de texto original (`orig_text`) hacía `m.get(...)` sobre `messages`,
  pero esa lista mezcla dicts con objetos `ChatCompletionMessage` (el mensaje del
  assistant). Se reemplazó por acceso seguro (isinstance dict → .get, si no → getattr).

- **bot.py — Fix loop "¿para qué día?" en eventos recurrentes.**
  "agendame todos los días a las 4 tomar el hierro" → "¿para qué día?" → "todos los
  días del año" → "¿para qué día?" (loop infinito, porque "todos los días del año" no
  tiene un día concreto). Ahora: (1) un evento con `repetir` sin `fecha` arranca HOY
  automáticamente y se excluye del branch que pregunta el día; (2) red de seguridad en
  el handler de `_pending_event_date`: si la respuesta del día no trae fecha reconocible
  y no es recurrente, se asume HOY en vez de re-preguntar.

- **Eventos recurrentes (Google RRULE) — "todos los días/cada semana/todos los meses".**
  Antes `create_event` creaba UN solo evento: si el usuario pedía una serie, el modelo
  o mentía ("agendé todos") o los creaba uno por uno mensaje a mensaje. Ahora:
  - `google_services.py`: `create_event` acepta `repetir` (diario/semanal/mensual/anual)
    y `hasta` (YYYY-MM-DD). Helper `_build_rrule` arma la RRULE con COUNT (evita líos de
    zona horaria). El resultado devuelve `repetir`/`hasta` para la confirmación.
  - `bot.py`: schema de `create_event` con params `repetir` (enum) y `hasta`; `_execute_tool`
    los pasa; `_format_event_confirmation` muestra "🔁 todos los días hasta el ...";
    regla nueva en el prompt ("UNA sola llamada con repetir, NUNCA día por día ni afirmar
    una serie sin repetir"); `_pending_event_date` ahora preserva repetir/hasta si hay que
    preguntar el día de inicio.

- **bot.py — Subida de modelo: gpt-4.1-mini → gpt-4.1.**
  `CHAT_MODEL` ahora es `os.getenv("CHAT_MODEL", "gpt-4.1")`. Se puede rollback a
  `gpt-4.1-mini` desde la env de Render sin re-deploy. Mismo API (function calling,
  visión, prompt caching), solo cambia el costo (~5x) y mejora la comprensión.

- **bot.py — Fix "agendame X" sin día inventaba la fecha de hoy.**
  Nuevo helper `_text_has_date_ref(text)` (junto a los `_is_*_intent`) que detecta
  si el mensaje menciona algún día/fecha (relativos, día de semana, mes, "el 15",
  15/07, "la semana que viene", etc.). El branch de `create_event` en
  `_run_tool_calls` ahora pregunta el día si la fecha falta **o** si el usuario no
  mencionó ninguna (aunque el modelo la haya inventado). Reusa el mecanismo
  `_pending_event_date`.

- **bot.py — Normalización de la respuesta de día.**
  En el handler de `_pending_event_date` (`_route_text`), si el usuario responde un
  número pelado ("15") se normaliza a "el 15" para que `_text_has_date_ref` lo
  reconozca y no vuelva a preguntar en loop.

- **bot.py — Confirmar TODAS las acciones cuando hay más de una en la misma tanda.**
  Antes, si en un mensaje se creaba un evento **y** un recordatorio, el `direct_reply`
  se armaba solo con la confirmación del evento y tapaba la del recordatorio (el
  recordatorio se creaba igual pero no se avisaba). Se agregó `extra_confirmations`
  (lista gemela de `event_confirmations`); el branch `add_reminder` ahora arma una
  confirmación determinística ("⏰ Le aviso ...") y al final se combinan todas.
  Nota: un recordatorio solo ahora también usa confirmación fija (antes la redactaba
  el modelo).
