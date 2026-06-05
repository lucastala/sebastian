# Bot de Tareas y Eventos — @Sebiss_bot

Bot de Telegram en Python para gestión de tareas y eventos desde el celular. Corre 24/7 en Render.

---

## Comandos

| Comando | Acción |
|---------|--------|
| `.texto` | Agrega una tarea nueva |
| `.número` | Elimina la tarea por número de lista |
| `.lista` | Muestra las tareas pendientes |
| `.hoy` | Muestra los eventos de hoy en Google Calendar |
| `.mañana` | Muestra los eventos de mañana |
| Audio de voz | Transcribe con Whisper y procesa como texto |
| Cualquier otro texto | OpenAI decide qué herramienta usar |

---

## Flujo al agregar una tarea

1. `.comprar leche` → tarea guardada en Google Sheets
2. Aparece un **calendario inline** para elegir fecha límite
3. Al seleccionar la fecha → se guarda y muestra la lista actualizada

---

## Lista de tareas

- Las tareas **sin fecha** aparecen arriba
- Las tareas **con fecha** aparecen abajo, ordenadas de más lejana a más próxima
- La más urgente queda abajo del todo (para leer de abajo hacia arriba)
- Las fechas aparecen en **negrita** antes del nombre de la tarea

Ejemplo:
```
📋 Tareas pendientes:
1. patente
2. llamar al contador
3. viernes 20 jun — turno con el médico
4. jueves 5 jun — mandar presupuesto
5. martes 3 jun — pagar monotributo
```

---

## Herramientas disponibles para OpenAI

OpenAI usa `gpt-4o-mini` con function calling y decide sola qué herramienta usar:

| Función | Descripción |
|---------|-------------|
| `get_today_events()` | Eventos de hoy en Google Calendar |
| `get_events_by_date(fecha)` | Eventos de una fecha específica |
| `search_event(query)` | Busca evento por nombre |
| `create_event(nombre, fecha, hora)` | Crea evento (si no hay hora → todo el día) |
| `get_pending_tasks()` | Lista tareas pendientes del Sheet |

---

## Resumen automático

Todos los días a las **8:00 AM (Argentina)** el bot manda automáticamente:

```
☀️ Buenos días! Acá tu resumen de hoy:

📅 Eventos de hoy:
- 10:00 Reunión con el contador

📋 Tareas pendientes:
1. llamar al médico
2. pagar monotributo
```

---

## Stack

| Componente | Tecnología |
|------------|------------|
| Bot | python-telegram-bot v22+ |
| IA | OpenAI gpt-4o-mini con function calling |
| Transcripción de audio | OpenAI Whisper |
| Tareas | Google Sheets (gspread) |
| Calendario | Google Calendar API |
| Scheduler | APScheduler |
| Deploy | Render (Background Worker) |
| Config | python-dotenv |

---

## Archivos

```
bot-tareas/
├── bot.py            ← código principal
├── requirements.txt
├── .python-version   ← fuerza Python 3.11.9 en Render
├── .gitignore        ← excluye .env
└── README.md
```

> El archivo `.env` con las credenciales **no se sube al repo**.

---

## Variables de entorno

Configuradas en el dashboard de Render:

```
TELEGRAM_TOKEN
OPENAI_API_KEY
GOOGLE_SHEETS_ID
GOOGLE_SHEET_NAME
GOOGLE_CREDENTIALS_JSON   ← JSON del service account en una sola línea
GOOGLE_CALENDAR_ID        ← email de la cuenta de Google Calendar
DAILY_SUMMARY_CHAT_ID
```

---

## Google Sheets

- **URL:** `https://docs.google.com/spreadsheets/d/1Gfq7EniEMCFFEuxaK9dayQUD4phURWU1iC9obMXJWec`
- **Hoja:** `sebastian`
- **Columnas:** `id` | `tarea` | `estado` | `prioridad` | `fecha`
- **Estados:** `pendiente` / `completada`

El service account `bot-tareas@bot-tareas-497818.iam.gserviceaccount.com` necesita acceso **Editor** al Sheet y **"Realizar cambios en eventos"** en Google Calendar.

---

## Correr en local

```bash
cd "proyecto sebastian"
pip install -r requirements.txt
# completar .env con las credenciales
python bot.py
```
