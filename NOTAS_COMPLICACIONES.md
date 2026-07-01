# Sebastian — Prompt de contexto: complicaciones recurrentes

> Pegá este documento al empezar una sesión para dar contexto completo de los problemas
> que venimos peleando y la dirección que tomamos. Complementa a `CHANGELOG.md` (detalle
> cronológico) — esto es el "por qué" y los patrones.

## Qué es Sebastian
Bot de Telegram (SaaS) en español rioplatense (trato de USTED, formal pero breve) para
productividad + finanzas personales: tareas, eventos de Google Calendar, recordatorios,
gastos, ingresos, deudas, cuotas/tarjeta, gastos fijos, lista del súper, listados con
nombre. Stack: python-telegram-bot v21 (polling), FastAPI/uvicorn (OAuth), APScheduler,
Supabase (Postgres), OpenAI (modelo actual: **gpt-4.1**, configurable por env `CHAT_MODEL`).
Deploy: push a `main` → Render deploya solo.

## LA CAUSA RAÍZ DE CASI TODO
El pipeline se diseñó para el modelo VIEJO (gpt-4o-mini, que no llamaba tools solo):
~25 detectores de keywords (`_is_*_intent`) adivinan la intención → **fuerzan** un
`tool_choice` → y **recortaban** las tools a una sola. Ese andamiaje, con gpt-4.1, ESTORBA:
- Los keywords se pisan entre sí por substrings y orden (ej. "lista del súper" contenía
  "la lista" → caía en tareas).
- Forzar/recortar una sola tool ROMPE los pedidos combinados (ej. evento + recordatorio:
  el modelo no tenía `add_reminder` disponible).
- Frágil ante sinónimos: cualquier forma nueva de decir algo se rompe.

**Regla de oro que fuimos descubriendo:**
- **Interpretar lo que el usuario quiere = trabajo del MODELO** (gpt-4.1 lo hace bien, incluso
  frases novедosas). NO hardcodear listas de frases para esto.
- **Renderizar listas y armar confirmaciones = trabajo del CÓDIGO** (determinístico, nunca
  falla, no "promete y no cumple").

Cada vez que sacamos una regla forzada y confiamos en el modelo con contexto + todas las
tools, el bug se arregla Y generaliza. Verificamos TODO con experimentos reales contra
gpt-4.1 (ver carpeta scratchpad: exp_*.py) antes de dar por buena una hipótesis.

## PATRONES DE BUG RECURRENTES (con ejemplos reales)

1. **"Prometo la lista y no la mando".** El modelo respondía "al final verá la lista..." y
   no mostraba nada (gastos, eventos, recordatorios). → Fix: render determinístico en código.
   Ya hecho para tareas, gastos, recordatorios, eventos.

2. **Ruteo por keywords que se pisa.** "mostrame la lista del súper" mostraba TAREAS porque
   el detector de tareas corría antes y "la lista" es substring. → Fix puntual (excluir
   súper/compras/listado) PERO la solución de fondo es sacar los `_is_*_intent`.

3. **Recorte de tools rompe combos.** "agendá X y recordame una hora antes" → solo creaba el
   evento (sin `add_reminder` disponible). → Fix: `_tools_for` manda SIEMPRE todas las tools;
   intents que combinan pasan a `"required"` (actuar, pero elegir libre) en vez de forzar 1.

4. **Acción cableada a un tipo.** El borrado forzaba `delete_event` siempre → borrar un
   recordatorio decía "Evento eliminado", y "borrá las dos cosas" borraba una sola. → Fix:
   `"required"` + todas las tools + prompt "evento vs recordatorio" → el modelo elige
   delete_event y/o cancel_reminder por contexto.

5. **El modelo inventa/omite parámetros bajo pedidos complejos.** Ej. "todos los días, 2 tomas,
   julio-septiembre" a veces salía sin recurrencia. Intentamos un guardrail por FRASES
   ("todos los días"...) y lo REVERTIMOS: era el anti-patrón frágil. La solución real fue el
   PROMPT enseñando el concepto (repetir/hasta) + 1 ejemplo trabajado → generaliza.

6. **Timezone.** La hora es SIEMPRE local Argentina; el modelo NO debe convertir a UTC
   ('11:50 am' → 11:50). Está en el prompt y en la descripción del tool. (El código de
   create_event ya estaba bien; el bug era el modelo.)

7. **Cosas que Google maneja distinto.** Eventos recurrentes = RRULE (una sola llamada crea
   la serie). Borrar una instancia de serie borra solo ese día → hay que borrar el evento
   MAESTRO (recurringEventId) para borrar toda la serie.

8. **Robustez de plataforma.** Faltaba error handler global (excepciones morían en silencio,
   "No error handlers are registered"). Mensajes > 4096 chars rompían Telegram. Markdown
   frágil con contenido del usuario (usar fallback a texto plano). Todo con try/except +
   `_reply_long` (parte en trozos + cae a plano).

## DIRECCIÓN / PENDIENTE
- **Refactor de fondo (el grande):** ir sacando los ~25 `_is_*_intent` y dejar que gpt-4.1
  maneje con el toolset completo. Ya migramos: reminder-add, borrado, y `_tools_for` (no
  recorta). Hacerlo de a poco, con experimento real por cada intent, NO de golpe (afecta
  gastos, deudas, súper, todo).
- **Canal WhatsApp** (competencia Memorae está ahí; Telegram tiene poca penetración en LatAm).
  Es la decisión estratégica más grande.
- **Freemium con límite** (ya hay MercadoPago; falta el gating).
- Costo: vigilar gasto de OpenAI con gpt-4.1 (~5x el mini). Rollback = env `CHAT_MODEL=gpt-4.1-mini`.

## REGLAS DE TRABAJO (del usuario, Lucas)
- Anotar TODO cambio en `CHANGELOG.md` antes de cerrarlo. No pisar trabajo previo.
- Pushear a `main` SIEMPRE tras cada cambio, sin preguntar (deploya a Render).
- Verificar hipótesis con experimentos reales contra el modelo, no suponer.
- Código prolijo, reusar patrones existentes, no tapar con parches. Ser honesto cuando algo
  se me escapó o me equivoqué.
