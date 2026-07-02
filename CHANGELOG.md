# Changelog — Sebastian

Registro de cambios para no pisar trabajo previo. Lo más nuevo arriba.

## 2026-07-01

- **bot.py — Botones de tareas alineados: tarea a la IZQUIERDA, estrellas a la DERECHA.**
  Telegram centra siempre el texto de los botones (la API no tiene alineación); el truco
  es rellenar con braille blank U+2800 (blanco que los clientes no recortan):
  `🗑️ 1. tarea⠀⠀⠀⠀⭐⭐⭐`. `_task_btn_label` + `_TASK_BTN_WIDTH = 35` (ancho
  aproximado: emoji/estrella rinden ~2 chars). Si en el teléfono se ve cortado con "…"
  o descentrado, ajustar esa constante.

- **Tareas v2: sin choclazo (la lista SON los botones) + prioridad con estrellas en vez
  de fecha límite.** (bot.py, data_store.py, database.py, scheduler.py, texts.py, schema.sql)
  ⚠️ **REQUIERE SQL EN SUPABASE** (schema.sql al final):
  `ALTER TABLE public.tareas ADD COLUMN IF NOT EXISTS prioridad INT NOT NULL DEFAULT 0;`
  + `NOTIFY pgrst, 'reload schema';` — hasta correrlo, elegir/pasar prioridad falla
  (ver la lista y borrar siguen andando).
  - **Sin choclazo:** el texto de la lista se fue; queda un encabezado corto con el
    conteo y cada tarea es su botón `🗑️ n. ⭐⭐⭐ tarea`. Orden: más estrellas arriba,
    a igual prioridad la más vieja primero (bot._sort_tasks == data_store._sort_pending,
    clave para que ".2" y delete_task borren lo que se ve).
  - **Prioridad 0-5 estrellas reemplaza a la fecha límite:** al agregar una tarea
    (".tarea" o lenguaje natural sin prioridad dicha) se muestran 6 botones
    (⭐…⭐⭐⭐⭐⭐ y "Sin estrellas", callback `tprio_{id}_{n}`) en lugar del calendario.
    Si el usuario la dice ("urgente", "5 estrellas"), add_task la recibe en `prioridad`
    y no se pregunta. update_task cambió `nueva_fecha` → `nueva_prioridad`.
    get_pending_tasks (tool) devuelve {n, tarea, prioridad}.
  - **Eliminado:** calendario de fecha límite (handle_cal_nav/day, _build_calendar_keyboard,
    update_task_fecha), config "Orden de tareas" (menu_orden, update_user_orden,
    orden_tareas ya no se usa — la columna puede quedar en la DB), y el footer de texto
    de tareas (build_tasks_footer): si una acción trae su propio teclado, la lista ya no
    viaja (la lista son botones).
  - Resumen diario (scheduler) ahora ordena igual que el bot y muestra las estrellas.
    Manuales de texts.py actualizados.
  - Verificado: mocks locales 7/7 (orden, labels, tprio, tdel, tool, vacío) y experimento
    REAL contra gpt-4.1 8/8 (exp_prioridad_modelo.py): sin mención → add_task SIN
    prioridad; "urgente/5 estrellas" → prioridad=5; "tarea para el viernes" → sigue
    siendo add_task (no evento); "ponele 3 estrellas a la tarea 2" →
    update_task(2, nueva_prioridad=3).

- **bot.py + data_store.py — Tareas con botones 🗑️ para eliminar (mismo patrón que los
  listados).**
  Antes borrar tareas era solo con ".2" o por lenguaje natural; ahora TODA lista de
  tareas (menú, ".tareas", ".N", pedido natural, footer tras acciones, foto) sale con un
  botón `🗑️ n. tarea` por tarea que la elimina al tocarlo y re-renderiza el mismo mensaje
  (como `menu_lidel_` en listados). Detalles:
  - `data_store.delete_task_by_id`: borra por **id de Supabase**, no por posición — el
    botón sigue apuntando a la MISMA tarea aunque la lista se reordene entre que se
    mostró y se tocó. Tap repetido sobre una ya borrada avisa "ya no estaba" y no rompe.
  - `build_tasks_view(user, menu)` reemplaza a `build_tasks_footer` en los lugares con
    botones (un solo fetch: texto y teclado siempre coinciden). El callback lleva el
    contexto (`tdel_{id}_{m|c}`) para re-renderizar con "Volver al menú" solo en el menú.
  - `build_tasks_footer` queda SOLO para cuando la lista acompaña una acción con teclado
    propio (ej. calendario de fecha límite): ahí va texto sin botones con el hint ".2".
  - Se eliminó `_tasks_help_kb` (el botón "Manual de uso" ahora va al pie del teclado de
    tareas). El borrado por ".2" y por lenguaje natural siguen funcionando igual.
  Verificado con experimento local con mocks (exp_tasks_botones.py, 5/5): orden
  texto/botones idéntico en ambos órdenes de usuario, borrado por id, contexto menú
  preservado tras borrar, tap repetido, lista vacía.

- **bot.py — Combo sin hora: el modelo agendaba igual (a veces inventando 09:00) y la
  pregunta '¿a qué hora?' se perdía.**
  Retest post-fix anterior: "agendame reunion el martes... y poneme un recordatorio una
  hora antes" (sin hora) creó el evento TODO EL DÍA sin recordatorio y sin preguntar nada.
  Experimento (4/4): el modelo creaba el evento en ronda 1 (3/4 inventando hora 09:00) y
  preguntaba la hora en ronda 2 COMO TEXTO... que la composición descartaba (regla
  last_round_had_direct). Dos fixes:
  1. **Prompt**: regla RECORDATORIO RELATIVO reforzada con ejemplo trabajado (patrón que
     generaliza): sin hora → NI create_event NI add_reminder, preguntar PRIMERO y crear
     los dos juntos al tener la hora. Verificado: ahora pregunta sin llamar tools.
  2. **Contrato 'Listo.'**: el texto final del modelo ya no se descarta; se agrega salvo
     que sea exactamente 'Listo.' (regla nueva en el prompt: 'Listo.' si todo tiene
     confirmación del sistema; frase breve si hizo algo sin confirmación de código, ej.
     cancel_reminder; SOLO la pregunta si falta info). Reemplaza la heurística
     last_round_had_direct, que perdía preguntas.
  Verificado end-to-end (exp_e2e_v2.py): pregunta la hora → 'a las 15' → ambas acciones
  con ambas confirmaciones; lista+cancelar conserva la confirmación; evento simple sin
  narración duplicada.

- **bot.py — Combo evento+recordatorio en dos turnos: el guard de fecha descartaba el evento
  y el loop cortaba los pedidos encadenados.**
  Síntoma real (transcript): "agendame el martes reunion de prueba y un recordatorio una
  hora antes" → "¿a qué hora?" → "a las 15" → solo se creaba el recordatorio; el evento
  NUNCA. Diagnóstico con experimentos reales (gpt-4.1 y minis: 17/17 emiten AMBAS tool
  calls — el modelo NO era el problema). Tres bugs de código encadenados:
  1. **Guard anti-"fecha inventada" miraba solo el ÚLTIMO mensaje** ("a las 15", sin
     fecha) en vez de toda la conversación ("el martes" venía de 2 mensajes antes) →
     descartaba el evento y preguntaba "¿para qué día?". Fix: `user_gave_date_ref`
     revisa TODOS los mensajes user de la ventana.
  2. **Las confirmaciones pisaban preguntas/listas de la misma tanda** (final de
     `_run_tool_calls`): la pregunta "¿para qué día agendo?" era reemplazada por
     "⏰ Le aviso..." → el usuario nunca la veía y el evento moría en `_pending_event_date`.
     Fix: se concatena TODO (confirmaciones + pregunta/lista).
  3. **Early-return del loop multi-ronda**: si una tool armaba direct_reply, se cortaba
     el turno → cualquier combo secuencial moría ("mostrame los recordatorios y cancelá
     el de X": mostraba la lista y el cancel nunca corría — verificado 4/4). Fix: los
     direct_reply se ACUMULAN entre rondas y el loop sigue hasta que el modelo no pida
     más tools; si la última ronda actuó sin respuesta de código, se suma el texto del
     modelo. `seen_event_sigs` ahora se comparte entre rondas (anti-duplicado).
     Costo: +1 llamada a OpenAI por turno con acción.
  Además: regla nueva en el prompt ("PEDIDOS CON VARIAS ACCIONES: emití TODAS las tool
  calls juntas; nunca completes solo una parte"). Verificado end-to-end con el loop real
  y tools mockeadas (exp_e2e_loop_arreglado.py): combo paralelo y secuencial OK.

- **bot.py — Borrado distingue evento vs recordatorio + listados deterministas.**
  Síntomas: (a) "qué eventos tengo el martes" a veces prometía y no listaba; (b)
  "eliminá las dos cosas" borraba solo el evento; (c) borrar un recordatorio decía
  "Evento eliminado". Causas: listados escritos por el modelo (poco fiables) y borrado
  forzado a delete_event. Fixes:
  - Render determinístico de recordatorios (`_format_recordatorios_lista`) y eventos
    (`_format_eventos_lista`); ramas get_reminders / get_today_events / get_events_by_date
    en `_run_tool_calls` los ponen como direct_reply.
  - `_is_event_delete_intent` pasa de forzar `delete_event` a `"required"`: con todas las
    tools el modelo elige delete_event y/o cancel_reminder según el contexto. Prompt con
    regla "evento vs recordatorio al borrar" + "borrá ambos si piden las dos cosas".
    Probado (exp_delete.py): "eliminá el de reunión" tras ver recordatorios → cancel_reminder;
    "eliminá las dos cosas" → delete_event + cancel_reminder.

- **bot.py — Error handler global + mensajes largos (crash al pedir gastos).**
  Al pedir la lista de gastos el bot tiró excepción no atrapada ("No error handlers
  are registered"). Causa probable: la lista superaba el límite de 4096 chars de
  Telegram y fallaban AMBOS intentos de envío (Markdown y plano). Fixes:
  (1) `error_handler` global registrado con `app.add_error_handler`: loguea el traceback
  COMPLETO y avisa al usuario en vez de morir en silencio. (2) `_reply_long`: parte los
  mensajes largos en trozos <4096 por líneas y cae a texto plano si el Markdown falla;
  reemplaza el bloque de envío del handler de texto.

- **bot.py — Evento + recordatorio en un mismo pedido (dejó de romperse).**
  "agendá reunión y recordame una hora antes" solo creaba el evento; el recordatorio
  nunca se hacía. Causa: `_tools_for` recortaba la lista a UNA sola tool (deuda del
  modelo viejo), así el modelo no tenía `add_reminder` disponible. Cambios:
  (1) `_tools_for` ahora SIEMPRE manda todas las tools (probado contra gpt-4.1: con hora
  crea create_event + add_reminder correctamente; los tools son estáticos → se cachean).
  (2) `_is_reminder_add_intent` pasa de forzar `add_reminder` a `"required"`, para que si
  además hay evento pueda crear ambos. (3) Prompt: recordatorio relativo a un evento sin
  hora → NO inventa ni agenda todo-el-día; pregunta a qué hora es (verificado: pregunta).
  Nota: primer paso concreto de sacar el andamiaje de "forzar una sola tool".

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
