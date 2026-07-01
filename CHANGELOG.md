# Changelog — Sebastian

Registro de cambios para no pisar trabajo previo. Lo más nuevo arriba.

## 2026-07-01

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
