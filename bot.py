import os
import json
import re
import logging
import tempfile
from datetime import datetime, timedelta, date

from dotenv import load_dotenv
load_dotenv()

import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from openai import OpenAI

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Env ───────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN       = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY       = os.getenv("OPENAI_API_KEY")
GOOGLE_SHEETS_ID     = os.getenv("GOOGLE_SHEETS_ID")
GOOGLE_SHEET_NAME    = os.getenv("GOOGLE_SHEET_NAME", "sebastian")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
GOOGLE_CALENDAR_ID    = os.getenv("GOOGLE_CALENDAR_ID", "primary")
DAILY_SUMMARY_CHAT_ID = int(os.getenv("DAILY_SUMMARY_CHAT_ID", "8589342013"))

TZ_ARG = pytz.timezone("America/Argentina/Buenos_Aires")

def escape_md(text: str) -> str:
    return re.sub(r'([_*\[\]()~`>#+=|{}.!\-])', r'\\\1', str(text))

# ── Google clients ────────────────────────────────────────────────────────────
_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/calendar",
]

def _get_credentials():
    creds_info = json.loads(GOOGLE_CREDENTIALS_JSON)
    return Credentials.from_service_account_info(creds_info, scopes=_SCOPES)

def _sheets_client():
    creds = _get_credentials()
    gc = gspread.authorize(creds)
    return gc.open_by_key(GOOGLE_SHEETS_ID).worksheet(GOOGLE_SHEET_NAME)

def _calendar_service():
    creds = _get_credentials()
    return build("calendar", "v3", credentials=creds, cache_discovery=False)

# ── Google Sheets helpers ─────────────────────────────────────────────────────
def sheets_get_pending() -> list[dict]:
    ws = _sheets_client()
    rows = ws.get_all_records()
    return [r for r in rows if str(r.get("estado", "")).strip().lower() == "pendiente"]

def sheets_add_task(text: str) -> dict:
    ws = _sheets_client()
    task_id = str(int(datetime.now().timestamp() * 1000))
    ws.append_row([task_id, text.strip(), "pendiente", "", ""])
    return {"id": task_id, "tarea": text.strip(), "estado": "pendiente"}

def sheets_update_fecha(task_id: str, fecha: str) -> bool:
    ws = _sheets_client()
    all_rows = ws.get_all_records()
    all_ids = [str(r.get("id", "")) for r in all_rows]
    if task_id not in all_ids:
        return False
    sheet_row = all_ids.index(task_id) + 2
    headers = ws.row_values(1)
    fecha_col = headers.index("fecha") + 1
    ws.update_cell(sheet_row, fecha_col, fecha)
    return True

def sheets_update_priority(task_id: str, stars: int) -> bool:
    ws = _sheets_client()
    all_rows = ws.get_all_records()
    all_ids = [str(r.get("id", "")) for r in all_rows]
    if task_id not in all_ids:
        return False
    sheet_row = all_ids.index(task_id) + 2
    headers = ws.row_values(1)
    col_name = "prioridad" if "prioridad" in headers else "fecha"
    prio_col = headers.index(col_name) + 1
    ws.update_cell(sheet_row, prio_col, stars)
    return True

def sheets_delete_task_by_position(pos: int) -> str | None:
    """pos is 1-based. Returns deleted task name or None if not found."""
    ws = _sheets_client()
    all_rows = ws.get_all_records()
    pending = [r for r in all_rows if str(r.get("estado", "")).strip().lower() == "pendiente"]
    pending = sorted(pending, key=lambda t: int(t.get("prioridad") or 0), reverse=False)
    if pos < 1 or pos > len(pending):
        return None
    target = pending[pos - 1]
    task_id = str(target["id"])
    # Find actual row index in sheet (1-based, +1 for header)
    all_ids = [str(r.get("id", "")) for r in all_rows]
    sheet_row = all_ids.index(task_id) + 2  # +2: header + 1-based
    estado_col = ws.row_values(1).index("estado") + 1
    ws.update_cell(sheet_row, estado_col, "completada")
    return target["tarea"]

# ── Google Calendar helpers ───────────────────────────────────────────────────
def _today_str() -> str:
    return datetime.now(TZ_ARG).strftime("%Y-%m-%d")

def _day_bounds(date_str: str):
    """Returns (start_rfc, end_rfc) for a full day in Argentina timezone."""
    day = TZ_ARG.localize(datetime.strptime(date_str, "%Y-%m-%d"))
    start = day.isoformat()
    end = (day + timedelta(days=1)).isoformat()
    return start, end

def cal_get_events_by_date(date_str: str) -> list[dict]:
    svc = _calendar_service()
    start, end = _day_bounds(date_str)
    result = svc.events().list(
        calendarId=GOOGLE_CALENDAR_ID,
        timeMin=start,
        timeMax=end,
        singleEvents=True,
        orderBy="startTime",
    ).execute()
    events = []
    for e in result.get("items", []):
        s = e["start"]
        hora = s.get("dateTime", s.get("date", ""))
        if "T" in hora:
            hora = datetime.fromisoformat(hora).astimezone(TZ_ARG).strftime("%H:%M")
        else:
            hora = "todo el día"
        events.append({"nombre": e.get("summary", ""), "hora": hora, "id": e["id"]})
    return events

def cal_get_today_events() -> list[dict]:
    return cal_get_events_by_date(_today_str())

def cal_search_event(query: str) -> list[dict]:
    svc = _calendar_service()
    now = datetime.now(TZ_ARG).isoformat()
    result = svc.events().list(
        calendarId=GOOGLE_CALENDAR_ID,
        q=query,
        timeMin=now,
        singleEvents=True,
        orderBy="startTime",
        maxResults=5,
    ).execute()
    events = []
    for e in result.get("items", []):
        s = e["start"]
        hora = s.get("dateTime", s.get("date", ""))
        if "T" in hora:
            hora = datetime.fromisoformat(hora).astimezone(TZ_ARG).strftime("%d/%m/%Y %H:%M")
        else:
            hora = datetime.strptime(hora, "%Y-%m-%d").strftime("%d/%m/%Y") + " (todo el día)"
        events.append({"nombre": e.get("summary", ""), "hora": hora, "id": e["id"]})
    return events

def cal_create_event(nombre: str, fecha: str, hora: str | None = None) -> dict:
    svc = _calendar_service()
    if hora:
        dt_str = f"{fecha}T{hora}:00"
        dt_start = TZ_ARG.localize(datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S"))
        dt_end = dt_start + timedelta(hours=1)
        event_body = {
            "summary": nombre,
            "start": {"dateTime": dt_start.isoformat(), "timeZone": "America/Argentina/Buenos_Aires"},
            "end":   {"dateTime": dt_end.isoformat(),   "timeZone": "America/Argentina/Buenos_Aires"},
        }
    else:
        event_body = {
            "summary": nombre,
            "start": {"date": fecha},
            "end":   {"date": fecha},
        }
    created = svc.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event_body).execute()
    return {"nombre": nombre, "fecha": fecha, "hora": hora or "todo el día", "id": created["id"]}

# ── OpenAI function calling ───────────────────────────────────────────────────
oai = OpenAI(api_key=OPENAI_API_KEY)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_today_events",
            "description": "Obtiene los eventos de hoy en Google Calendar.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_events_by_date",
            "description": "Obtiene los eventos de una fecha específica.",
            "parameters": {
                "type": "object",
                "properties": {
                    "fecha": {"type": "string", "description": "Fecha en formato YYYY-MM-DD"}
                },
                "required": ["fecha"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_event",
            "description": "Busca eventos en Google Calendar por nombre o descripción.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Texto a buscar"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_event",
            "description": "Crea un evento en Google Calendar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "nombre": {"type": "string", "description": "Nombre del evento"},
                    "fecha":  {"type": "string", "description": "Fecha en formato YYYY-MM-DD"},
                    "hora":   {"type": "string", "description": "Hora en formato HH:MM, o null si es todo el día"},
                },
                "required": ["nombre", "fecha"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_pending_tasks",
            "description": "Lee las tareas pendientes de Google Sheets.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]

def _dispatch_tool(name: str, args: dict) -> str:
    if name == "get_today_events":
        events = cal_get_today_events()
        if not events:
            return "No hay eventos hoy."
        return "\n".join(f"- {e['hora']} {e['nombre']}" for e in events)

    if name == "get_events_by_date":
        events = cal_get_events_by_date(args["fecha"])
        if not events:
            return f"No hay eventos el {args['fecha']}."
        return "\n".join(f"- {e['hora']} {e['nombre']}" for e in events)

    if name == "search_event":
        events = cal_search_event(args["query"])
        if not events:
            return "No encontré eventos con ese nombre."
        return "\n".join(f"- {e['hora']} {e['nombre']}" for e in events)

    if name == "create_event":
        ev = cal_create_event(args["nombre"], args["fecha"], args.get("hora"))
        hora_txt = ev["hora"] if ev["hora"] != "todo el día" else "todo el día"
        return f"Evento creado: {ev['nombre']} el {ev['fecha']} a las {hora_txt}."

    if name == "get_pending_tasks":
        tasks = sheets_get_pending()
        if not tasks:
            return "No tenés tareas pendientes."
        return "\n".join(f"{i+1}. {t['tarea']}" for i, t in enumerate(tasks))

    return "Herramienta desconocida."

def openai_process(user_text: str) -> str:
    today = datetime.now(TZ_ARG).strftime("%Y-%m-%d")
    messages = [
        {
            "role": "system",
            "content": (
                f"Sos un asistente personal. Hoy es {today} (zona horaria America/Argentina/Buenos_Aires). "
                "Usá las herramientas disponibles para responder. Respondé siempre en español, de forma concisa. "
                "Cuando creés un evento y el usuario dice una hora en punto ('a las 4', 'a las 10'), "
                "usá siempre :00 como minutos (ej: 16:00, 10:00). Nunca uses los minutos actuales del reloj."
            ),
        },
        {"role": "user", "content": user_text},
    ]

    while True:
        response = oai.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
        )
        msg = response.choices[0].message

        if not msg.tool_calls:
            return msg.content or ""

        messages.append(msg)
        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments)
            result = _dispatch_tool(tc.function.name, args)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })

# ── Transcribe audio ──────────────────────────────────────────────────────────
def transcribe_audio(file_path: str) -> str:
    with open(file_path, "rb") as f:
        transcript = oai.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            language="es",
        )
    return transcript.text

# ── Pending tasks footer ───────────────────────────────────────────────────────
def build_tasks_footer() -> str:
    tasks = sheets_get_pending()
    if not tasks:
        lines = ["📋 *Tareas pendientes:*", "_No hay tareas pendientes\\._"]
    else:
        tasks_sorted = sorted(tasks, key=lambda t: int(t.get("prioridad") or 0), reverse=False)
        lines = ["📋 *Tareas pendientes:*"]
        for i, t in enumerate(tasks_sorted, 1):
            stars = int(t.get("prioridad") or 0)
            star_str = ("⭐" * stars + " ") if stars > 0 else ""
            fecha = str(t.get("fecha", "")).strip()
            fecha_str = ""
            if fecha:
                try:
                    fecha_str = " — " + datetime.strptime(fecha, "%Y-%m-%d").strftime("%d/%m")
                except Exception:
                    fecha_str = f" — {fecha}"
            lines.append(f"{i}\\. {star_str}{escape_md(t['tarea'])}{escape_md(fecha_str)}")
    lines.append("")
    lines.append("_Usá \\.texto para agregar tarea\\. Usá \\.número para eliminar\\._")
    return "\n".join(lines)

# ── Core message logic ────────────────────────────────────────────────────────
async def process_text(text: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = text.strip()

    # .número → eliminar tarea
    m = re.match(r"^\.(\d+)$", text)
    if m:
        pos = int(m.group(1))
        deleted = sheets_delete_task_by_position(pos)
        if deleted:
            reply = f"✅ Tarea *{pos}* eliminada: _{escape_md(deleted)}_\n\n"
        else:
            reply = f"⚠️ No existe la tarea número {pos}\\.\n\n"
        await update.message.reply_text(
            reply + build_tasks_footer(), parse_mode="MarkdownV2"
        )
        return

    # .texto → agregar tarea
    m = re.match(r"^\.\s*(.+)$", text)
    if m:
        task_text = m.group(1).strip()
        task = sheets_add_task(task_text)
        keyboard = [
            [
                InlineKeyboardButton("⭐",     callback_data=f"prio_{task['id']}_1"),
                InlineKeyboardButton("⭐⭐",   callback_data=f"prio_{task['id']}_2"),
                InlineKeyboardButton("⭐⭐⭐", callback_data=f"prio_{task['id']}_3"),
                InlineKeyboardButton("⭐⭐⭐⭐",   callback_data=f"prio_{task['id']}_4"),
                InlineKeyboardButton("⭐⭐⭐⭐⭐", callback_data=f"prio_{task['id']}_5"),
            ],
            [InlineKeyboardButton("Sin prioridad", callback_data=f"prio_{task['id']}_0")],
        ]
        await update.message.reply_text(
            f"✅ Tarea agregada: _{escape_md(task_text)}_\n\n¿Qué prioridad le ponés?",
            parse_mode="MarkdownV2",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    # Cualquier otro texto → OpenAI function calling
    ai_reply = openai_process(text)
    await update.message.reply_text(
        escape_md(ai_reply) + "\n\n" + build_tasks_footer(), parse_mode="MarkdownV2"
    )

# ── Telegram handlers ─────────────────────────────────────────────────────────
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await process_text(update.message.text, update, context)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    voice = update.message.voice
    tg_file = await context.bot.get_file(voice.file_id)
    tmp_path = os.path.join(tempfile.gettempdir(), f"voice_{voice.file_id}.ogg")
    await tg_file.download_to_drive(tmp_path)
    try:
        transcribed = transcribe_audio(tmp_path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    await update.message.reply_text(f"🎙 _Transcripción:_ {escape_md(transcribed)}", parse_mode="MarkdownV2")
    await process_text(transcribed, update, context)

# ── Priority callback ─────────────────────────────────────────────────────────
async def handle_priority_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    logger.info(f"Callback recibido: {query.data}")
    await query.answer()
    try:
        parts = query.data.split("_", 2)
        task_id, stars_str = parts[1], parts[2]
        stars = int(stars_str)
        sheets_update_priority(task_id, stars)
        star_display = "⭐" * stars if stars > 0 else "sin prioridad"
        keyboard = [[
            InlineKeyboardButton("Hoy",      callback_data=f"fecha_{task_id}_hoy"),
            InlineKeyboardButton("Mañana",   callback_data=f"fecha_{task_id}_manana"),
            InlineKeyboardButton("En 7 días", callback_data=f"fecha_{task_id}_semana"),
            InlineKeyboardButton("Sin fecha", callback_data=f"fecha_{task_id}_ninguna"),
        ]]
        await query.edit_message_text(
            f"✅ Prioridad: {escape_md(star_display)}\\. ¿Fecha límite?",
            parse_mode="MarkdownV2",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception as e:
        logger.error(f"Error en priority callback: {e}")
        await query.edit_message_text(f"❌ Error al guardar prioridad: {e}")

# ── Fecha callback ────────────────────────────────────────────────────────────
async def handle_fecha_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    logger.info(f"Fecha callback: {query.data}")
    await query.answer()
    try:
        parts = query.data.split("_", 2)
        task_id, tipo = parts[1], parts[2]
        if tipo != "ninguna":
            d = datetime.now(TZ_ARG)
            if tipo == "manana":
                d += timedelta(days=1)
            elif tipo == "semana":
                d += timedelta(days=7)
            fecha = d.strftime("%Y-%m-%d")
            sheets_update_fecha(task_id, fecha)
            msg = f"✅ Fecha límite: {escape_md(d.strftime('%d/%m'))}\n\n"
        else:
            msg = "✅ Sin fecha límite\n\n"
        await query.edit_message_text(msg + build_tasks_footer(), parse_mode="MarkdownV2")
    except Exception as e:
        logger.error(f"Error en fecha callback: {e}")
        await query.edit_message_text(f"❌ Error: {e}")

# ── Daily summary ─────────────────────────────────────────────────────────────
async def send_daily_summary(bot):
    events = cal_get_today_events()
    tasks  = sheets_get_pending()

    if events:
        ev_lines = ["📅 *Eventos de hoy:*"]
        for e in events:
            ev_lines.append(f"\\- {escape_md(e['hora'])} {escape_md(e['nombre'])}")
        events_block = "\n".join(ev_lines)
    else:
        events_block = "📅 *Eventos de hoy:* _ninguno_"

    if tasks:
        t_lines = ["📋 *Tareas pendientes:*"]
        for i, t in enumerate(tasks, 1):
            t_lines.append(f"{i}\\. {escape_md(t['tarea'])}")
        tasks_block = "\n".join(t_lines)
    else:
        tasks_block = "📋 *Tareas pendientes:* _ninguna_"

    msg = f"☀️ *Buenos días\\!* Acá tu resumen de hoy:\n\n{events_block}\n\n{tasks_block}"
    await bot.send_message(chat_id=DAILY_SUMMARY_CHAT_ID, text=msg, parse_mode="MarkdownV2")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(CallbackQueryHandler(handle_priority_callback, pattern=r"^prio_"))
    app.add_handler(CallbackQueryHandler(handle_fecha_callback, pattern=r"^fecha_"))

    scheduler = AsyncIOScheduler(timezone=TZ_ARG)
    scheduler.add_job(
        lambda: app.create_task(send_daily_summary(app.bot)),
        trigger="cron",
        hour=8,
        minute=0,
    )
    scheduler.start()

    logger.info("Bot iniciado.")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
