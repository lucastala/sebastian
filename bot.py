import calendar as cal_module
import io
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from openai import AsyncOpenAI
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from database import create_user, get_user
from google_services import (
    add_task,
    create_event,
    delete_event,
    delete_task_by_position,
    get_events_by_date,
    get_pending_tasks,
    get_today_events,
    search_event,
    update_task_fecha,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

PAYMENT_LINK = os.getenv("PAYMENT_LINK", "https://tu-link-de-pago.com")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
ARGENTINA_TZ = timezone(timedelta(hours=-3))

DIAS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES_ES = ["", "ene", "feb", "mar", "abr", "may", "jun",
            "jul", "ago", "sep", "oct", "nov", "dic"]

OPENAI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_today_events",
            "description": "Obtiene los eventos de hoy del Google Calendar del usuario",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_events_by_date",
            "description": "Obtiene los eventos de una fecha específica del Google Calendar",
            "parameters": {
                "type": "object",
                "properties": {
                    "fecha": {
                        "type": "string",
                        "description": "Fecha en formato YYYY-MM-DD",
                    }
                },
                "required": ["fecha"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_event",
            "description": "Busca un evento por nombre o descripción en Google Calendar",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Término de búsqueda del evento",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_event",
            "description": "Crea un nuevo evento en Google Calendar",
            "parameters": {
                "type": "object",
                "properties": {
                    "nombre": {"type": "string", "description": "Título del evento"},
                    "fecha": {
                        "type": "string",
                        "description": "Fecha en formato YYYY-MM-DD",
                    },
                    "hora": {
                        "type": "string",
                        "description": (
                            "Hora en formato HH:MM (opcional). "
                            "Si no se provee se crea como evento de todo el día."
                        ),
                    },
                },
                "required": ["nombre", "fecha"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_pending_tasks",
            "description": "Obtiene las tareas pendientes del usuario desde Google Sheets",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_delete_event",
            "description": (
                "Muestra un botón de confirmación para eliminar un evento. "
                "Usar cuando el usuario quiere eliminar un evento del calendario."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "string",
                        "description": "ID del evento a eliminar",
                    },
                    "event_name": {
                        "type": "string",
                        "description": "Nombre del evento",
                    },
                    "event_time": {
                        "type": "string",
                        "description": "Hora o fecha del evento para mostrar al usuario",
                    },
                },
                "required": ["event_id", "event_name"],
            },
        },
    },
]


# ── Date helpers ──────────────────────────────────────────────────────────────

def _format_fecha(fecha_str: str) -> str:
    """Returns e.g. 'martes 3' or 'martes 3 jun' if different month."""
    try:
        d = datetime.strptime(fecha_str, "%Y-%m-%d")
        hoy = datetime.now(ARGENTINA_TZ)
        nombre = DIAS_ES[d.weekday()]
        if d.month == hoy.month and d.year == hoy.year:
            return f"{nombre} {d.day}"
        return f"{nombre} {d.day} {MESES_ES[d.month]}"
    except Exception:
        return fecha_str


def _sort_tasks(tasks: list[dict]) -> list[dict]:
    """No-date tasks first, then dated tasks most-distant→most-recent (top→bottom)."""
    no_date = [t for t in tasks if not str(t.get("fecha", "")).strip()]
    dated = sorted(
        [t for t in tasks if str(t.get("fecha", "")).strip()],
        key=lambda t: str(t["fecha"]),
        reverse=True,
    )
    return no_date + dated


# ── Calendar keyboard ─────────────────────────────────────────────────────────

def _build_calendar_keyboard(task_id: str, year: int, month: int) -> InlineKeyboardMarkup:
    MESES = ["", "Enero", "Feb", "Mar", "Abr", "May", "Jun",
             "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
    prev_m = month - 1 if month > 1 else 12
    prev_y = year if month > 1 else year - 1
    next_m = month + 1 if month < 12 else 1
    next_y = year if month < 12 else year + 1

    rows = []
    rows.append([
        InlineKeyboardButton("◀", callback_data=f"calNav_{task_id}_{prev_y}_{prev_m:02d}"),
        InlineKeyboardButton(f"{MESES[month]} {year}", callback_data="calIgnore"),
        InlineKeyboardButton("▶", callback_data=f"calNav_{task_id}_{next_y}_{next_m:02d}"),
    ])
    rows.append([
        InlineKeyboardButton(d, callback_data="calIgnore")
        for d in ["Lu", "Ma", "Mi", "Ju", "Vi", "Sa", "Do"]
    ])

    now = datetime.now(ARGENTINA_TZ)
    for week in cal_module.monthcalendar(year, month):
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(" ", callback_data="calIgnore"))
            else:
                is_today = (day == now.day and month == now.month and year == now.year)
                label = f"[{day}]" if is_today else str(day)
                row.append(InlineKeyboardButton(
                    label,
                    callback_data=f"calDay_{task_id}_{year}-{month:02d}-{day:02d}",
                ))
        rows.append(row)

    rows.append([InlineKeyboardButton("Sin fecha →", callback_data=f"calDay_{task_id}_ninguna")])
    return InlineKeyboardMarkup(rows)


# ── Tasks footer ──────────────────────────────────────────────────────────────

async def build_tasks_footer(user: dict) -> str:
    try:
        tasks = await get_pending_tasks(user)
    except Exception as e:
        logger.error(f"Error fetching tasks for user {user.get('chat_id')}: {e}")
        return "⚠️ No se pudieron cargar las tareas pendientes."

    if not tasks:
        return "No tenés tareas pendientes.\n\nUsá .texto para agregar tarea."

    lines = ["📋 *Tareas pendientes:*"]
    for i, task in enumerate(_sort_tasks(tasks), 1):
        fecha = str(task.get("fecha", "")).strip()
        if fecha:
            lines.append(f"{i}. *{_format_fecha(fecha)}* — {task['tarea']}")
        else:
            lines.append(f"{i}. {task['tarea']}")
    lines.append("\nUsá .texto para agregar tarea. Usá .número para eliminar.")
    return "\n".join(lines)


# ── OpenAI ────────────────────────────────────────────────────────────────────

async def _execute_tool(func_name: str, func_args: dict, user: dict):
    if func_name == "get_today_events":
        return await get_today_events(user)
    if func_name == "get_events_by_date":
        return await get_events_by_date(user, func_args["fecha"])
    if func_name == "search_event":
        return await search_event(user, func_args["query"])
    if func_name == "create_event":
        return await create_event(
            user, func_args["nombre"], func_args["fecha"], func_args.get("hora")
        )
    if func_name == "get_pending_tasks":
        return await get_pending_tasks(user)
    return {"error": f"Función desconocida: {func_name}"}


async def _call_openai(
    user: dict, text: str
) -> tuple[str, InlineKeyboardMarkup | None]:
    today = datetime.now(ARGENTINA_TZ).strftime("%Y-%m-%d")
    messages = [
        {
            "role": "system",
            "content": (
                "Sos un asistente personal de productividad. "
                "Ayudás a gestionar tareas y eventos de Google Calendar. "
                "Respondé en español rioplatense, de forma concisa y amigable. "
                f"La fecha de hoy es {today}. "
                "Si el usuario menciona días relativos (mañana, el lunes, etc.), "
                "calculá la fecha correcta a partir de hoy. "
                "Cuando el usuario pide una hora en punto ('a las 4', 'a las 10'), "
                "usá siempre HH:00 como minutos. "
                "Cuando el usuario quiera eliminar un evento, primero buscalo con "
                "search_event o get_events_by_date para obtener su ID, y luego "
                "usá propose_delete_event para mostrarle la confirmación."
            ),
        },
        {"role": "user", "content": text},
    ]

    response = await openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        tools=OPENAI_TOOLS,
        tool_choice="auto",
    )

    msg = response.choices[0].message

    if not msg.tool_calls:
        return msg.content or "No pude procesar tu mensaje.", None

    messages.append(msg)
    pending_keyboard: InlineKeyboardMarkup | None = None

    for tc in msg.tool_calls:
        func_name = tc.function.name
        func_args = json.loads(tc.function.arguments)
        logger.info(f"Tool call: {func_name}({func_args}) for user {user['chat_id']}")

        if func_name == "propose_delete_event":
            event_id = func_args["event_id"]
            event_name = func_args.get("event_name", "evento")
            event_time = func_args.get("event_time", "")
            label = f"🗑️ Sí, eliminar — {event_name}"
            if event_time:
                label = f"🗑️ Sí, eliminar — {event_name} ({event_time})"
            pending_keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(label, callback_data=f"delEvent_{event_id}"),
                InlineKeyboardButton("❌ No", callback_data="delEventCancel"),
            ]])
            messages.append({
                "tool_call_id": tc.id,
                "role": "tool",
                "name": func_name,
                "content": "Confirmación mostrada al usuario.",
            })
        else:
            result = await _execute_tool(func_name, func_args, user)
            messages.append({
                "tool_call_id": tc.id,
                "role": "tool",
                "name": func_name,
                "content": json.dumps(result, ensure_ascii=False, default=str),
            })

    final = await openai_client.chat.completions.create(
        model="gpt-4o-mini", messages=messages
    )
    return final.choices[0].message.content or "Listo.", pending_keyboard


async def _transcribe_voice(voice_bytes: bytes) -> str:
    buf = io.BytesIO(voice_bytes)
    buf.name = "audio.ogg"
    result = await openai_client.audio.transcriptions.create(
        model="whisper-1", file=buf, language="es"
    )
    return result.text


# ── Calendar callbacks ────────────────────────────────────────────────────────

async def handle_cal_nav(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.data == "calIgnore":
        return
    _, task_id, year, month = query.data.split("_", 3)
    await query.edit_message_reply_markup(
        reply_markup=_build_calendar_keyboard(task_id, int(year), int(month))
    )


async def handle_cal_day(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    user = await get_user(chat_id)
    if not user:
        return

    _, task_id, fecha_val = query.data.split("_", 2)

    if fecha_val != "ninguna":
        await update_task_fecha(user, task_id, fecha_val)
        header = f"✅ Fecha límite: *{_format_fecha(fecha_val)}*\n\n"
    else:
        header = "✅ Sin fecha límite\n\n"

    footer = await build_tasks_footer(user)
    await query.edit_message_text(header + footer, parse_mode="Markdown")


# ── Message routing ───────────────────────────────────────────────────────────

async def _route_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user: dict, text: str
) -> None:
    message = update.message

    if text.startswith("."):
        content = text[1:].strip()

        if re.match(r"^\d+$", content):
            pos = int(content)
            deleted_name = await delete_task_by_position(user, pos)
            prefix = (
                f"✅ Eliminada: *{deleted_name}*\n\n"
                if deleted_name is not None
                else f"⚠️ No encontré la tarea #{pos}.\n\n"
            )
            footer = await build_tasks_footer(user)
            await message.reply_text(prefix + footer, parse_mode="Markdown")

        elif content:
            task_id = await add_task(user, content)
            if task_id:
                now = datetime.now(ARGENTINA_TZ)
                keyboard = _build_calendar_keyboard(task_id, now.year, now.month)
                await message.reply_text(
                    f"✅ Tarea agregada: *{content}*\n\n¿Fecha límite?",
                    parse_mode="Markdown",
                    reply_markup=keyboard,
                )
            else:
                await message.reply_text(
                    "⚠️ No se pudo agregar la tarea. "
                    "Completá la configuración de Google primero."
                )
        else:
            footer = await build_tasks_footer(user)
            await message.reply_text(
                "⚠️ Usá .texto para agregar o .número para eliminar.\n\n" + footer,
                parse_mode="Markdown",
            )
        return

    # OpenAI function calling
    try:
        reply, keyboard = await _call_openai(user, text)
    except Exception as e:
        logger.error(f"OpenAI error for user {user['chat_id']}: {e}")
        reply, keyboard = "⚠️ Tuve un error procesando tu mensaje. Intentá de nuevo.", None

    footer = await build_tasks_footer(user)
    await message.reply_text(
        reply + "\n\n" + footer,
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


# ── Delete event confirmation callback ───────────────────────────────────────

async def handle_delete_event(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.data == "delEventCancel":
        await query.edit_message_text("❌ Cancelado.")
        return

    event_id = query.data.split("_", 1)[1]
    chat_id = update.effective_chat.id
    user = await get_user(chat_id)
    if not user:
        return

    try:
        await delete_event(user, event_id)
        await query.edit_message_text("✅ Evento eliminado.")
    except Exception as e:
        logger.error(f"Error deleting event {event_id}: {e}")
        await query.edit_message_text("⚠️ No se pudo eliminar el evento.")


# ── Main handlers ─────────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message:
        return

    chat_id = update.effective_chat.id
    user = await get_user(chat_id)

    if user is None:
        await _start_onboarding(update)
        return

    if not user.get("access_token"):
        oauth_url = f"{BASE_URL}/oauth/start?chat_id={chat_id}"
        await message.reply_text(
            f"⚠️ Todavía no conectaste tu cuenta de Google.\n\n"
            f"Completá la configuración aquí:\n{oauth_url}"
        )
        return

    if user.get("estado_suscripcion") not in ("activo", "trial"):
        await message.reply_text(
            f"⚠️ Tu suscripción no está activa.\n\nActivá tu plan aquí:\n{PAYMENT_LINK}"
        )
        return

    await message.reply_chat_action("typing")

    if message.voice:
        try:
            voice_file = await message.voice.get_file()
            raw = await voice_file.download_as_bytearray()
            text = await _transcribe_voice(bytes(raw))
            await message.reply_text(f"🗣️ Transcripción: {text}")
        except Exception as e:
            logger.error(f"Voice transcription error for user {chat_id}: {e}")
            await message.reply_text("⚠️ No pude transcribir el audio. Intentá de nuevo.")
            return
    else:
        text = message.text or ""

    if not text.strip():
        return

    await _route_text(update, context, user, text)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = await get_user(chat_id)

    if user and user.get("access_token"):
        await update.message.reply_text(
            "👋 ¡Hola! Ya estás configurado y listo.\n\n"
            "Podés decirme:\n"
            "• .texto → agregar tarea\n"
            "• .número → eliminar tarea por número\n"
            "• 'qué tengo hoy' → ver eventos del día\n"
            "• 'crear reunión el viernes a las 10' → agregar evento\n"
            "• Audio de voz 🎤 → lo transcribo y proceso"
        )
    else:
        await _start_onboarding(update)


async def _start_onboarding(update: Update) -> None:
    chat_id = update.effective_chat.id
    nombre = update.effective_user.first_name or "Usuario"

    if not await get_user(chat_id):
        await create_user(chat_id, nombre)

    oauth_url = f"{BASE_URL}/oauth/start?chat_id={chat_id}"
    await update.message.reply_text(
        f"👋 ¡Hola {nombre}! Bienvenido a tu asistente personal.\n\n"
        f"Para empezar, necesito conectar tu cuenta de Google. "
        f"Esto me da acceso a tu Calendar y crea tu hoja de tareas en Google Sheets.\n\n"
        f"👉 Hacé clic aquí para autorizar:\n{oauth_url}\n\n"
        f"Una vez que completes la autorización, ¡ya podés usar el bot!"
    )


async def _post_init(application: Application) -> None:
    from scheduler import setup_scheduler

    scheduler = setup_scheduler(application.bot)
    scheduler.start()
    application.bot_data["scheduler"] = scheduler
    logger.info("Scheduler started — daily summary at 08:00 Argentina time")


async def _post_shutdown(application: Application) -> None:
    scheduler = application.bot_data.get("scheduler")
    if scheduler and scheduler.running:
        scheduler.shutdown()
        logger.info("Scheduler stopped")


def main() -> None:
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_TOKEN not set in .env")

    app = (
        Application.builder()
        .token(token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT | filters.VOICE, handle_message))
    app.add_handler(CallbackQueryHandler(handle_cal_nav, pattern=r"^calNav_|^calIgnore$"))
    app.add_handler(CallbackQueryHandler(handle_cal_day, pattern=r"^calDay_"))
    app.add_handler(CallbackQueryHandler(handle_delete_event, pattern=r"^delEvent"))

    logger.info("Bot starting — polling for updates")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
