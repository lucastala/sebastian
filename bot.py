import base64
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

from database import (
    activate_user,
    add_reminder,
    create_oauth_flow,
    delete_reminder,
    get_active_users,
    get_user,
    get_user_reminders,
    update_user_resumen,
    use_activation_code,
)
from texts import INSTRUCCIONES_TEXTO, MANUAL_TAREAS
from google_services import (
    GoogleAuthExpiredError,
    create_event,
    delete_event,
    get_events_by_date,
    get_today_events,
    search_event,
    update_event,
)
from data_store import (
    add_debt,
    add_expense,
    add_fixed_expense,
    add_income,
    add_list_items,
    add_super_item,
    add_task,
    cancel_fixed_expense,
    clear_super_list,
    delete_expense,
    delete_list,
    delete_task_by_position,
    get_balance,
    get_debts,
    get_expenses,
    get_fixed_expenses,
    get_list_items,
    get_list_names,
    get_pending_tasks,
    get_super_list,
    remove_list_items,
    remove_super_items,
    settle_debt,
    update_expense_monto,
    update_task,
    update_task_fecha,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Chat/vision model — change here to swap models everywhere
CHAT_MODEL = "gpt-4.1-mini"

# Max chained tool rounds per message (search→update, etc.) before forcing a reply
MAX_TOOL_ROUNDS = 5

PAYMENT_LINK = os.getenv("PAYMENT_LINK", "https://tu-link-de-pago.com")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
SUBSCRIBE_URL = os.getenv("SUBSCRIBE_URL", "https://www.chatsebastian.com")
ARGENTINA_TZ = timezone(timedelta(hours=-3))

# Código de activación: SEB- + 5 alfanuméricos
_CODE_RE = re.compile(r"^SEB-[A-Z0-9]{5}$", re.IGNORECASE)

WELCOME_NUEVO = (
    "👋 ¡Hola! Soy Sebastian, tu asistente personal.\n\n"
    "Si ya tenés tu código de activación, escribilo acá.\n\n"
    f"Para suscribirte visitá: {SUBSCRIBE_URL}"
)
INACTIVO_MSG = (
    "⚠️ Tu suscripción no está activa.\n\n"
    "Si tenés un código de activación, escribilo acá.\n\n"
    f"Para renovar visitá: {SUBSCRIBE_URL}"
)


def _looks_like_code(text: str) -> bool:
    return bool(_CODE_RE.match(text.strip()))

DIAS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES_ES = ["", "ene", "feb", "mar", "abr", "may", "jun",
            "jul", "ago", "sep", "oct", "nov", "dic"]

EXPENSE_CATEGORIES = [
    "Supermercado", "Restaurantes y delivery", "Transporte", "Vivienda",
    "Salud", "Ropa y calzado", "Suscripciones", "Entretenimiento", "Trabajo", "Otros",
]
CATEGORIA_EMOJI = {
    "Supermercado": "🛒", "Restaurantes y delivery": "🍕", "Transporte": "⛽",
    "Vivienda": "🏠", "Salud": "💊", "Ropa y calzado": "👕", "Suscripciones": "📱",
    "Entretenimiento": "🎉", "Trabajo": "💼", "Otros": "📦",
}

# Conversation memory — last N exchanges per user (in-memory, resets on restart)
_conversation_history: dict[int, list[dict]] = {}
MAX_HISTORY_EXCHANGES = 8  # 8 user+assistant pairs = 16 messages

# Pending event creates waiting for conflict confirmation
_pending_event_creates: dict[int, dict] = {}

# Chats currently in "add to supermarket list" mode (everything they send is an item)
_super_add_mode: set[int] = set()

# Chats in "add to named list" mode → maps chat_id to the list name being filled
_list_add_mode: dict[int, str] = {}

# Chats that just tapped "Nuevo listado" and we're waiting for them to type the name
_awaiting_list_name: set[int] = set()


def _split_items(text: str) -> list[str]:
    """Split a free-form list into items (by line, comma or 'y')."""
    parts = re.split(r"[,\n]+|\s+y\s+|\s+e\s+", text)
    return [p.strip(" -•*\t") for p in parts if p.strip(" -•*\t")]


def _within_minutes(inicio: str, target: datetime, window: timedelta) -> bool:
    try:
        ev_dt = datetime.fromisoformat(inicio)
        return abs((ev_dt - target).total_seconds()) <= window.total_seconds()
    except Exception:
        return False


def _get_history(chat_id: int) -> list[dict]:
    return list(_conversation_history.get(chat_id, []))


def _add_to_history(chat_id: int, user_msg: str, assistant_msg: str) -> None:
    history = _conversation_history.setdefault(chat_id, [])
    history.append({"role": "user", "content": user_msg})
    history.append({"role": "assistant", "content": assistant_msg})
    if len(history) > MAX_HISTORY_EXCHANGES * 2:
        _conversation_history[chat_id] = history[-(MAX_HISTORY_EXCHANGES * 2):]


_DELETE_STEMS = ("elimin", "borr", "quita", "quitá", "sacá", "sacame", "remov")
_TASK_WORDS = ("tarea", "tareas")
_RENAME_TASK_STEMS = ("renombr", "renombrá")
_EDIT_TASK_PHRASES = ("cambiá la tarea", "cambia la tarea", "editá la tarea", "edita la tarea",
                      "cambiá el nombre de la tarea", "cambia el nombre de la tarea",
                      "ponele fecha", "poné fecha", "pone fecha", "agregale fecha",
                      "cambiá la fecha de la tarea", "cambia la fecha de la tarea",
                      "cambiá la fecha", "cambia la fecha")
_EDIT_EVENT_STEMS = ("cambiá el evento", "cambia el evento", "editá el evento", "edita el evento",
                     "cambiá la reunión", "cambia la reunión", "cambiá la hora", "cambia la hora",
                     "pasá el evento", "mové el evento", "cambiá el turno", "cambia el turno")
_EXPENSE_ADD_STEMS = (
    "gasté", "gaste", "pagué", "pague", "compré", "compre",
    "me salió", "me salio", "me costó", "me costo", "gasto de",
)
_EXPENSE_QUERY_PHRASES = (
    "cuánto gasté", "cuanto gaste", "cuánto gaste", "cuanto gasté",
    "cuánto llevo", "cuanto llevo", "cuánto gasto", "cuanto gasto",
    "mis gastos", "ver gastos", "ver mis gastos",
    "mostrame los gastos", "mostrame mis gastos", "mostrá mis gastos",
    "resumen de gastos", "resumen de mis gastos",
    "en qué gasté", "en que gaste", "en qué gaste", "en que gasté",
)
_FIXED_QUERY_PHRASES = (
    "mis gastos fijos", "cuáles son mis gastos fijos", "cuales son mis gastos fijos",
    "ver gastos fijos", "ver mis gastos fijos", "qué gastos fijos", "que gastos fijos",
    "lista de gastos fijos", "mostrame los gastos fijos", "mostrame mis gastos fijos",
    "mis fijos", "cuáles son mis fijos", "cuales son mis fijos",
)
_FIXED_ADD_MARKERS = (
    "gasto fijo", "por mes", "todos los meses", "cada mes",
    "mensual", "mensualmente", "al mes",
)
_FIXED_CANCEL_VERBS = (
    "cancelá", "cancela", "sacá", "saca", "eliminá", "elimina",
    "dar de baja", "dá de baja", "da de baja", "borrá", "borra", "quitá", "quita",
)
_TASK_ADD_VERBS = (
    "agregame", "agregá", "agrega ", "añadí", "anadi", "añade", "añadime",
    "anotá", "anota", "anotame", "sumá", "sumame", "agregar",
    "recordame", "recordá", "recorda", "recuérdame", "recuerdame",
    "acordate", "acordá", "acorda", "no te olvides", "no me olvides",
    "agendame la tarea", "poné en la lista", "pone en la lista", "ponme en la lista",
)
_TASK_CONTEXT = ("tarea", "tareas", "lista", "pendiente", "pendientes")
# A specific time or one of these words means it's a calendar event, not a task
_EVENT_WORDS = ("reunión", "reunion", "evento", "cita", "turno", "junta")
_INCOME_ADD_STEMS = (
    "cobré", "cobre", "me pagaron", "me pagó", "me pago", "me depositaron",
    "me deposito", "me depositó", "ingresó", "ingreso de", "recibí", "recibi",
    "me transfirieron", "me transfirió", "me entró", "me entro",
)
_BALANCE_PHRASES = (
    "balance", "mi saldo", "saldo del mes", "cuánto me queda", "cuanto me queda",
    "cuánto tengo", "cuanto tengo", "cómo voy este mes", "como voy este mes",
    "cuánto gané", "cuanto gane", "cuánto ahorré", "cuanto ahorre",
)
_EXPENSE_DELETE_VERBS = ("borrá", "borra", "eliminá", "elimina", "sacá", "saca", "quitá", "quita")
_EXPENSE_EDIT_MARKERS = (
    "cambiá", "cambia", "corregí", "corregi", "actualizá", "actualiza",
    "modificá", "modifica", "el monto", "poné", "pone", "ponele",
)


def _is_event_delete_intent(text: str) -> bool:
    t = text.lower()
    has_delete = any(t.startswith(w) or f" {w}" in t for w in _DELETE_STEMS)
    has_task = any(w in t for w in _TASK_WORDS)
    return has_delete and not has_task


def _is_task_edit_intent(text: str) -> bool:
    t = text.lower()
    has_rename = any(t.startswith(w) or f" {w}" in t for w in _RENAME_TASK_STEMS)
    has_edit_phrase = any(p in t for p in _EDIT_TASK_PHRASES)
    has_task = any(w in t for w in _TASK_WORDS)
    # "la tarea N, ponele/cambiá..." — task number + modification verb
    has_tarea_num = bool(re.search(r"tarea\s+\d", t))
    return has_rename or has_edit_phrase or (has_tarea_num and has_task)


_EVENT_EDIT_VERBS = (
    "cambiá", "cambia", "cambiame", "modificá", "modifica", "modificame",
    "editá", "edita", "mové", "move", "pasá", "pasa", "corré", "corre",
)
_EVENT_NOUNS = ("evento", "reunión", "reunion", "turno", "cita", "junta")


def _is_event_edit_intent(text: str) -> bool:
    t = text.lower()
    if any(p in t for p in _EDIT_EVENT_STEMS):
        return True
    # edit verb + an event noun (ej. "cambiame la reunión a las 11")
    return any(v in t for v in _EVENT_EDIT_VERBS) and any(n in t for n in _EVENT_NOUNS)


# Imperative "agendá…" forms — strong signal of a NEW calendar event. We avoid the
# bare noun "agenda" ("mi agenda") and past forms ("agendado") on purpose.
_EVENT_CREATE_VERBS = (
    "agendá", "agendame", "agéndame", "agendámelo", "agendamelo", "agendalo",
    "agendala", "agendar", "creá un evento", "crea un evento", "crear un evento",
    "creame un evento", "creáme un evento", "nuevo evento", "nueva reunión",
    "nueva reunion", "nuevo turno",
)
_EVENT_NOUN_CREATE_VERBS = (
    "poné", "pone", "poneme", "ponme", "agregá", "agrega", "agregame",
    "anotá", "anota", "anotame", "meté", "mete", "sumá", "creá", "crea",
)


def _is_event_create_intent(text: str) -> bool:
    t = text.lower()
    # Never grab edits/deletes of an existing event.
    if _is_event_edit_intent(text) or _is_event_delete_intent(text):
        return False
    if any(v in t for v in _EVENT_CREATE_VERBS):
        return True
    # "poné/agregá/anotá una reunión/turno/cita ..." → new event
    return any(n in t for n in _EVENT_NOUNS) and any(v in t for v in _EVENT_NOUN_CREATE_VERBS)


def _is_expense_query_intent(text: str) -> bool:
    t = text.lower()
    return any(p in t for p in _EXPENSE_QUERY_PHRASES)


def _is_expense_add_intent(text: str) -> bool:
    t = text.lower()
    has_verb = any(s in t for s in _EXPENSE_ADD_STEMS)
    has_number = bool(re.search(r"\d", t))
    return has_verb and has_number


def _is_task_add_intent(text: str) -> bool:
    t = text.lower()
    # If it names an event or has a specific time, it's a calendar event, not a task
    if any(e in t for e in _EVENT_WORDS):
        return False
    if re.search(r"\ba las\s*\d|\b\d{1,2}\s*hs|\b\d{1,2}:\d{2}", t):
        return False
    # An explicit "add to list" verb is enough — no need for the word "tarea"
    if any(v in t for v in _TASK_ADD_VERBS):
        return True
    # Otherwise require the verb + the task context word
    return any(c in t for c in _TASK_CONTEXT) and "agreg" in t


_TASK_LIST_PHRASES = (
    "mostrame la lista", "mostrá la lista", "mostra la lista", "mostrame las tareas",
    "mostrame mis tareas", "mostrá mis tareas", "ver la lista", "ver mis tareas",
    "ver tareas", "ver las tareas", "mis tareas", "mis pendientes",
    "qué tareas tengo", "que tareas tengo", "lista de tareas", "mostrame los pendientes",
    "mostrame la lista de tareas", "dame la lista", "pasame la lista", "pasame las tareas",
)


def _is_task_list_request(text: str) -> bool:
    t = text.lower().strip().strip(".!?¿¡ ")
    # A message that is essentially just "tareas"/"lista"/"pendientes" = show the list
    if t in ("tareas", "tarea", "lista", "pendientes", "pendiente", "mis pendientes"):
        return True
    if "gasto" in t or "fijo" in t:
        return False
    # "agregame en la lista de tareas X" is an ADD, not a request to view the list
    if any(v in t for v in _TASK_ADD_VERBS):
        return False
    return any(p in t for p in _TASK_LIST_PHRASES)


def _is_menu_request(text: str) -> bool:
    t = text.lower()
    return "menú" in t or re.search(r"\bmenu\b", t) is not None


def _is_income_add_intent(text: str) -> bool:
    t = text.lower()
    has_verb = any(s in t for s in _INCOME_ADD_STEMS)
    return has_verb and bool(re.search(r"\d", t))


def _is_balance_intent(text: str) -> bool:
    t = text.lower()
    return any(p in t for p in _BALANCE_PHRASES)


# ── Deudas ────────────────────────────────────────────────────────────────────
_DEBT_QUERY_PHRASES = (
    "mis deudas", "cuánto debo", "cuanto debo", "a quién le debo", "a quien le debo",
    "quién me debe", "quien me debe", "qué deudas", "que deudas", "cuánto me deben",
    "cuanto me deben", "ver deudas", "ver mis deudas", "mostrame las deudas", "lista de deudas",
)
_DEBT_ADD_PHRASES = ("le debo", "les debo", "yo debo", "me debe", "me deben", "le debe")


def _is_debt_query_intent(text: str) -> bool:
    t = text.lower()
    return any(p in t for p in _DEBT_QUERY_PHRASES)


def _is_debt_add_intent(text: str) -> bool:
    t = text.lower()
    has = any(p in t for p in _DEBT_ADD_PHRASES) or bool(re.search(r"\bdebo\s", t))
    return has and bool(re.search(r"\d", t))


# ── Lista de supermercado ─────────────────────────────────────────────────────
_SUPER_WORDS = ("súper", "super", "supermercado", "lista de compras", "lista de la compra")
_SUPER_ADD_VERBS = (
    "agregá", "agrega", "agregame", "anotá", "anota", "anotame", "sumá", "suma", "sumame",
    "poné", "pone", "ponme", "añadí", "añade", "comprar", "comprá", "comprame", "falta", "necesito",
)
_SUPER_QUERY_PHRASES = (
    "lista del súper", "lista del super", "lista de compras", "lista de la compra",
    "qué tengo que comprar", "que tengo que comprar", "qué falta comprar", "que falta comprar",
    "mostrame la lista del súper", "mostrame la lista del super", "ver la lista del súper",
    "qué hay en el súper", "que hay en el super",
)


def _is_super_query_intent(text: str) -> bool:
    t = text.lower()
    return any(p in t for p in _SUPER_QUERY_PHRASES)


def _is_super_add_intent(text: str) -> bool:
    t = text.lower()
    if not any(w in t for w in _SUPER_WORDS):
        return False
    return any(v in t for v in _SUPER_ADD_VERBS)


# ── Listados con nombre ───────────────────────────────────────────────────────
_LIST_CREATE_TRIGGERS = (
    "armame un listado", "armame una lista", "armame la lista",
    "armá un listado", "arma un listado", "armá una lista", "arma una lista",
    "hacé un listado", "hace un listado", "hacé una lista", "hace una lista",
    "nuevo listado", "nueva lista", "creá un listado", "crea un listado",
    "creá una lista", "crea una lista", "cargá un listado", "empezá un listado",
)
_LISTS_QUERY_PHRASES = (
    "mis listados", "qué listados", "que listados", "ver mis listados",
    "mostrame mis listados", "cuáles son mis listados", "cuales son mis listados",
    "mis listas", "ver listados",
)


def _is_list_create_intent(text: str) -> bool:
    t = text.lower()
    if any(w in t for w in ("súper", "super", "compras", "tarea")):
        return False
    return any(p in t for p in _LIST_CREATE_TRIGGERS)


def _is_lists_query_intent(text: str) -> bool:
    t = text.lower().strip().strip(".!?¿¡ ")
    if t in ("listados", "mis listados"):
        return True
    return any(p in t for p in _LISTS_QUERY_PHRASES)


# ── Recordatorios con hora ────────────────────────────────────────────────────
_REMINDER_VERBS = (
    "recordame", "recordá", "recorda", "recuérdame", "recuerdame",
    "avisame", "avísame", "avisá", "avisame que", "hacéme acordar", "haceme acordar",
)
_REMINDER_QUERY_PHRASES = (
    "mis recordatorios", "qué recordatorios", "que recordatorios",
    "ver recordatorios", "mostrame los recordatorios",
)


def _has_time(text: str) -> bool:
    return bool(re.search(r"\ba las\s*\d|\b\d{1,2}\s*(hs|horas|am|pm)\b|\b\d{1,2}:\d{2}", text.lower()))


def _is_reminder_add_intent(text: str) -> bool:
    t = text.lower()
    return any(v in t for v in _REMINDER_VERBS) and _has_time(t)


def _is_reminder_query_intent(text: str) -> bool:
    t = text.lower().strip().strip(".!?¿¡ ")
    if t in ("recordatorios", "mis recordatorios"):
        return True
    return any(p in t for p in _REMINDER_QUERY_PHRASES)


def _is_expense_delete_intent(text: str) -> bool:
    t = text.lower()
    if "fijo" in t:
        return False
    has_verb = any(v in t for v in _EXPENSE_DELETE_VERBS)
    return has_verb and "gasto" in t and bool(re.search(r"\d", t))


def _is_task_delete_intent(text: str) -> bool:
    t = text.lower()
    if "gasto" in t or "fijo" in t or "evento" in t:
        return False
    has_verb = any(v in t for v in _EXPENSE_DELETE_VERBS)
    has_task = "tarea" in t or "tareas" in t
    return has_verb and has_task and bool(re.search(r"\d", t))


def _is_expense_edit_intent(text: str) -> bool:
    t = text.lower()
    if "fijo" in t:
        return False
    has_marker = any(m in t for m in _EXPENSE_EDIT_MARKERS)
    return has_marker and "gasto" in t and bool(re.search(r"\d", t))


def _is_fixed_query_intent(text: str) -> bool:
    t = text.lower()
    return any(p in t for p in _FIXED_QUERY_PHRASES)


def _is_fixed_cancel_intent(text: str) -> bool:
    t = text.lower()
    has_verb = any(v in t for v in _FIXED_CANCEL_VERBS)
    return has_verb and "fijo" in t


def _is_fixed_add_intent(text: str) -> bool:
    t = text.lower()
    has_marker = any(m in t for m in _FIXED_ADD_MARKERS)
    return has_marker and bool(re.search(r"\d", t))


# Pronoun-based deletes ("eliminalo", "borrá eso") depend on conversation context,
# not keywords — resolve them by looking at what was just discussed.
_PRONOUN_DELETE_FORMS = (
    "eliminalo", "eliminala", "eliminalos", "eliminalas",
    "borralo", "borrala", "borralos", "borralas",
    "sacalo", "sacala", "sacalos", "sacalas",
    "quitalo", "quitala", "quitalos", "quitalas",
    "eliminá eso", "elimina eso", "eliminá ese", "elimina ese",
    "borrá eso", "borra eso", "borrá ese", "borra ese",
    "sacá eso", "saca eso",
)


def _is_pronoun_delete(text: str) -> bool:
    t = text.lower()
    # If it names an explicit object + number (e.g. "las tareas 14 y 8"), it's not
    # an ambiguous bare pronoun — let the specific delete intents handle it.
    if re.search(r"\b(tareas?|gastos?|eventos?|reuni[óo]n)\b", t) and re.search(r"\d", t):
        return False
    return any(f in t for f in _PRONOUN_DELETE_FORMS)


def _last_topic(chat_id: int) -> str | None:
    """Infer whether recent conversation was about an expense or an event."""
    for msg in reversed(_get_history(chat_id)):
        c = str(msg.get("content", "")).lower()
        if "gasto" in c:
            return "expense"
        if "evento" in c or "reunión" in c or "reunion" in c or "turno" in c:
            return "event"
    return None

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
                            "Hora de inicio en formato HH:MM 24h (opcional). Copiá los minutos "
                            "exactos que diga el usuario, sin redondear. Si solo da una franja del "
                            "día, traducila (mañana=09:00, mediodía=13:00, tarde=16:00, noche=20:00). "
                            "Si no hay ninguna referencia horaria, omitila: será evento de todo el día."
                        ),
                    },
                    "hora_fin": {
                        "type": "string",
                        "description": (
                            "Hora de fin en formato HH:MM 24h (opcional). Usala cuando el usuario "
                            "da un rango, ej. 'de 19 a 20' → hora=19:00, hora_fin=20:00."
                        ),
                    },
                    "duracion_min": {
                        "type": "integer",
                        "description": (
                            "Duración en minutos (opcional). Usala cuando el usuario da una "
                            "duración, ej. 'reunión de 2 horas' → 120, 'media hora' → 30. "
                            "Si no se da ni hora_fin ni duracion_min, dura 1 hora."
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
            "name": "add_task",
            "description": (
                "Agrega una tarea pendiente a la lista del usuario. Usalo cuando el usuario pida "
                "agregar/anotar/sumar una tarea o un pendiente en lenguaje natural. "
                "Si menciona una fecha límite (mañana, el jueves, el 11, etc.), calculá la fecha exacta."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tarea": {"type": "string", "description": "Texto de la tarea"},
                    "fecha": {
                        "type": "string",
                        "description": "Fecha límite en formato YYYY-MM-DD (opcional)",
                    },
                },
                "required": ["tarea"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_task",
            "description": (
                "Elimina una o varias tareas pendientes por su NÚMERO en la lista (el mismo número "
                "que ve el usuario). Usalo cuando pida borrar/eliminar/sacar tareas por su número. "
                "Si pide eliminar varias, pasalas TODAS juntas en 'posiciones' (ej. [8, 14]). "
                "Si dudás del número, usá get_pending_tasks (cada tarea trae su 'n')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "posiciones": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Números de las tareas a eliminar, ej. [8, 14]",
                    },
                },
                "required": ["posiciones"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_event",
            "description": (
                "Busca un evento en el calendario por nombre y fecha, y muestra "
                "un botón de confirmación para eliminarlo. "
                "Llamá esta función directamente cuando el usuario quiera eliminar un evento."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Nombre o descripción del evento a eliminar",
                    },
                    "fecha": {
                        "type": "string",
                        "description": "Fecha del evento en formato YYYY-MM-DD (opcional pero recomendado)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_event",
            "description": (
                "Edita un evento del Google Calendar EN UN SOLO PASO. Pasá el nombre del evento en "
                "'query' (y opcionalmente 'fecha' para ubicarlo) y los cambios. El sistema busca el "
                "evento solo; NO uses search_event antes. Podés cambiar nombre, fecha y/o hora."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Nombre o descripción del evento a editar",
                    },
                    "fecha": {
                        "type": "string",
                        "description": "Fecha actual del evento YYYY-MM-DD, para ubicarlo (opcional)",
                    },
                    "nuevo_nombre": {
                        "type": "string",
                        "description": "Nuevo nombre/título del evento (opcional)",
                    },
                    "nueva_fecha": {
                        "type": "string",
                        "description": "Nueva fecha en formato YYYY-MM-DD (opcional)",
                    },
                    "nueva_hora": {
                        "type": "string",
                        "description": "Nueva hora en formato HH:MM (opcional)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_task",
            "description": (
                "Edita una tarea pendiente por su número de posición en la lista. "
                "Podés cambiar el nombre y/o la fecha. "
                "Usá get_pending_tasks primero si necesitás saber el número de la tarea."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "posicion": {
                        "type": "integer",
                        "description": "Número de posición de la tarea en la lista (empieza en 1)",
                    },
                    "nuevo_nombre": {
                        "type": "string",
                        "description": "Nuevo nombre de la tarea (opcional)",
                    },
                    "nueva_fecha": {
                        "type": "string",
                        "description": "Nueva fecha en formato YYYY-MM-DD, o cadena vacía para quitar la fecha (opcional)",
                    },
                },
                "required": ["posicion"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_expense",
            "description": (
                "Registra un gasto del usuario. Usá esta función cuando el usuario diga "
                "que gastó, pagó, compró o le costó algo de dinero (verbos en pasado). "
                "Inferí la categoría más apropiada según la descripción. "
                "Si menciona 'el lunes', 'ayer', etc., calculá la fecha; si no, usá hoy."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "monto": {"type": "number", "description": "Monto del gasto en pesos (solo el número)"},
                    "categoria": {
                        "type": "string",
                        "enum": EXPENSE_CATEGORIES,
                        "description": "Categoría del gasto",
                    },
                    "descripcion": {
                        "type": "string",
                        "description": (
                            "Descripción breve del gasto (máx ~6 palabras). Conservá el detalle "
                            "útil que dé el usuario (ej. 'uber a lo de mi novia'). Si el usuario da "
                            "una descripción muy larga, resumila a lo esencial."
                        ),
                    },
                    "fecha": {
                        "type": "string",
                        "description": "Fecha en formato YYYY-MM-DD (opcional, por defecto hoy)",
                    },
                },
                "required": ["monto", "categoria"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_expenses",
            "description": (
                "Consulta y lista los gastos del usuario en un período y/o categoría. "
                "Devuelve el total, el desglose por categoría y la lista de gastos "
                "(campo 'gastos', ya ordenada del más reciente al más antiguo, cada uno con su "
                "número 'n'). Cuando muestres la lista, enumerá los gastos EN EL MISMO ORDEN del "
                "array, usando el número 'n', con su descripción y monto. "
                "Calculá las fechas desde/hasta a partir de hoy si el usuario menciona un período "
                "relativo (este mes, esta semana, hoy, etc.). Para 'este mes' usá desde el día 1."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "desde": {"type": "string", "description": "Fecha inicio YYYY-MM-DD (opcional)"},
                    "hasta": {"type": "string", "description": "Fecha fin YYYY-MM-DD (opcional)"},
                    "categoria": {
                        "type": "string",
                        "enum": EXPENSE_CATEGORIES,
                        "description": "Filtrar por una categoría (opcional)",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_expense_monto",
            "description": (
                "Cambia el monto de un gasto ya registrado, identificándolo por su número en la "
                "lista. Pasá los MISMOS filtros (categoria/desde/hasta) que se usaron para mostrar "
                "la lista, para que el número coincida. Si no hay filtros, es la lista general."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "posicion": {"type": "integer", "description": "Número del gasto en la lista mostrada"},
                    "nuevo_monto": {"type": "number", "description": "Nuevo monto en pesos"},
                    "categoria": {"type": "string", "enum": EXPENSE_CATEGORIES,
                                  "description": "Mismo filtro de categoría usado al listar (opcional)"},
                    "desde": {"type": "string", "description": "Mismo desde usado al listar (opcional)"},
                    "hasta": {"type": "string", "description": "Mismo hasta usado al listar (opcional)"},
                },
                "required": ["posicion", "nuevo_monto"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_expense",
            "description": (
                "Elimina un gasto ya registrado, identificándolo por su número en la lista. "
                "Pasá los MISMOS filtros (categoria/desde/hasta) que se usaron para mostrar la "
                "lista, para que el número coincida."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "posicion": {"type": "integer", "description": "Número del gasto en la lista mostrada"},
                    "categoria": {"type": "string", "enum": EXPENSE_CATEGORIES,
                                  "description": "Mismo filtro de categoría usado al listar (opcional)"},
                    "desde": {"type": "string", "description": "Mismo desde usado al listar (opcional)"},
                    "hasta": {"type": "string", "description": "Mismo hasta usado al listar (opcional)"},
                },
                "required": ["posicion"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_fixed_expense",
            "description": (
                "Registra un gasto fijo mensual (se carga solo cada mes): alquiler, seguro, "
                "patente, cuota de club, suscripciones, etc. Usalo cuando el usuario declare un "
                "gasto recurrente ('el alquiler son 200000 por mes', 'agregá gasto fijo: ...'). "
                "Inferí la categoría. Si dice un día del mes ('el 10'), usalo; si no, 1."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "nombre": {"type": "string", "description": "Nombre del gasto fijo (ej. Alquiler, Netflix, Seguro auto)"},
                    "monto": {"type": "number", "description": "Monto mensual en pesos"},
                    "categoria": {"type": "string", "enum": EXPENSE_CATEGORIES, "description": "Categoría"},
                    "dia_del_mes": {"type": "integer", "description": "Día del mes en que se paga (1-28, por defecto 1)"},
                },
                "required": ["nombre", "monto", "categoria"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_fixed_expenses",
            "description": "Lista los gastos fijos mensuales activos del usuario, enumerados con su monto y día.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_fixed_expense",
            "description": (
                "Da de baja un gasto fijo mensual para que deje de cargarse, identificándolo por "
                "su nombre. Usalo cuando el usuario diga 'cancelá el gasto fijo X', 'dá de baja X', etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "nombre": {"type": "string", "description": "Nombre (o parte) del gasto fijo a cancelar"},
                },
                "required": ["nombre"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_income",
            "description": (
                "Registra un ingreso/cobro del usuario. Usalo cuando diga que YA cobró, le pagaron, "
                "recibió o le depositaron dinero (verbo en pasado, con un monto). "
                "Si menciona una fecha calculala; si no, usá hoy."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "monto": {"type": "number", "description": "Monto del ingreso en pesos"},
                    "descripcion": {"type": "string", "description": "De qué fue el ingreso (sueldo, venta, etc.)"},
                    "fecha": {"type": "string", "description": "Fecha YYYY-MM-DD (opcional, por defecto hoy)"},
                },
                "required": ["monto"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_balance",
            "description": (
                "Calcula el balance del usuario en un período: total de ingresos, total de gastos y "
                "el neto (ingresos − gastos). Usalo cuando pregunte por su balance, saldo, cuánto le "
                "queda o cómo viene el mes. Calculá desde/hasta según el período (para 'este mes', "
                "desde el día 1 del mes actual hasta hoy)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "desde": {"type": "string", "description": "Fecha inicio YYYY-MM-DD (opcional)"},
                    "hasta": {"type": "string", "description": "Fecha fin YYYY-MM-DD (opcional)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_debt",
            "description": (
                "Registra una deuda. Usalo cuando el usuario diga que LE DEBE plata a alguien "
                "('le debo 30000 a Vero', 'debo 5000 a Juan') → tipo='debo'; o que alguien le debe "
                "('Caro me debe 4000', 'me deben 2000') → tipo='me_deben'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "persona": {"type": "string", "description": "Nombre de la persona"},
                    "monto": {"type": "number", "description": "Monto en pesos"},
                    "tipo": {"type": "string", "enum": ["debo", "me_deben"],
                             "description": "'debo' si el usuario debe, 'me_deben' si le deben"},
                },
                "required": ["persona", "monto", "tipo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_debts",
            "description": "Lista las deudas del usuario (lo que debe y lo que le deben) con los totales.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "settle_debt",
            "description": (
                "Salda (elimina) una o varias deudas por su NÚMERO en la lista. Usalo cuando el "
                "usuario diga que pagó/saldó una deuda o que ya le pagaron. Pasá los números en "
                "'posiciones' (ej. [1, 3])."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "posiciones": {"type": "array", "items": {"type": "integer"},
                                   "description": "Números de las deudas a saldar"},
                },
                "required": ["posiciones"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_super_item",
            "description": (
                "Agrega un producto a la lista de supermercado/compras. Usalo cuando el usuario pida "
                "agregar algo a la lista del súper o de compras ('agregá leche a la lista del súper', "
                "'para el súper: pan y huevos'). Si menciona varios productos, llamá la función una "
                "vez por cada producto."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "item": {"type": "string", "description": "Producto a agregar"},
                },
                "required": ["item"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_super_list",
            "description": "Muestra la lista de supermercado/compras del usuario, numerada.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_super_items",
            "description": (
                "Quita uno o varios productos de la lista del súper por su NÚMERO. Usalo cuando el "
                "usuario diga que ya compró algo o quiere sacarlo de la lista. Pasá los números en "
                "'posiciones'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "posiciones": {"type": "array", "items": {"type": "integer"},
                                   "description": "Números de los productos a quitar"},
                },
                "required": ["posiciones"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clear_super_list",
            "description": "Vacía por completo la lista de supermercado. Usalo cuando pida borrar/vaciar toda la lista.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_to_list",
            "description": (
                "Agrega ítems a un listado con nombre (distinto del súper y de las tareas). "
                "Usalo cuando el usuario pida armar/crear/agregar a un listado nombrado, ej. "
                "'armame una lista de viaje con protector y ojotas', 'agregá pilas al listado de "
                "la mudanza'. Si no da ítems todavía, igual creá el listado con items vacío y "
                "pedile qué quiere agregar."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "nombre": {"type": "string", "description": "Nombre del listado (ej. viaje, mudanza, regalos)"},
                    "items": {"type": "array", "items": {"type": "string"},
                              "description": "Ítems a agregar (puede ser vacío)"},
                },
                "required": ["nombre", "items"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_list",
            "description": "Muestra los ítems de un listado nombrado. Ej. 'mostrame el listado del viaje'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "nombre": {"type": "string", "description": "Nombre del listado a mostrar"},
                },
                "required": ["nombre"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_lists",
            "description": "Lista los nombres de todos los listados del usuario con su cantidad de ítems.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_list_items",
            "description": "Quita ítems de un listado nombrado, por su número. Pasá los números en 'posiciones'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "nombre": {"type": "string", "description": "Nombre del listado"},
                    "posiciones": {"type": "array", "items": {"type": "integer"},
                                   "description": "Números de los ítems a quitar"},
                },
                "required": ["nombre", "posiciones"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_list",
            "description": "Borra por completo un listado nombrado. Usalo cuando pida eliminar/borrar todo el listado.",
            "parameters": {
                "type": "object",
                "properties": {
                    "nombre": {"type": "string", "description": "Nombre del listado a borrar"},
                },
                "required": ["nombre"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_reminder",
            "description": (
                "Programa un recordatorio para AVISAR a una hora específica. Usalo cuando el "
                "usuario pida que le recuerdes/avises algo a una hora ('recordame llamar al médico "
                "mañana a las 3', 'avisame a las 18 que saque la carne'). Calculá la fecha y la hora."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "texto": {"type": "string", "description": "Qué recordar"},
                    "fecha": {"type": "string", "description": "Fecha YYYY-MM-DD (por defecto hoy)"},
                    "hora": {"type": "string", "description": "Hora HH:MM (24h)"},
                },
                "required": ["texto", "hora"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_reminders",
            "description": "Lista los recordatorios pendientes del usuario (los que todavía no se avisaron).",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_reminder",
            "description": "Cancela uno o varios recordatorios pendientes por su número en la lista. Pasá los números en 'posiciones'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "posiciones": {"type": "array", "items": {"type": "integer"},
                                   "description": "Números de los recordatorios a cancelar"},
                },
                "required": ["posiciones"],
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


def _format_event_confirmation(ev: dict) -> str:
    """Confirmación determinística de un evento creado, armada con los datos REALES que
    devolvió Google (hora exacta + link). No la escribe el modelo: así nunca redondea
    ni dice que agendó algo que no se creó."""
    nombre = ev.get("nombre") or "Evento"
    link = ev.get("link")
    if ev.get("all_day"):
        fecha = (ev.get("inicio") or "")[:10]
        cuando = f"{_format_fecha(fecha)} · todo el día"
    else:
        inicio = ev.get("inicio", "")
        fin = ev.get("fin", "")
        h1, h2 = inicio[11:16], fin[11:16]
        base = _format_fecha(inicio[:10])
        cuando = f"{base} · {h1} a {h2}" if h2 else f"{base} · {h1}"
    txt = f"✅ *Agendado: {nombre}*\n📅 {cuando}"
    if link:
        txt += f"\n🔗 [Ver en Google Calendar]({link})"
    return txt


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
        logger.error(
            f"Error fetching tasks for user {user.get('chat_id')}: {type(e).__name__}: {e}"
        )
        return "⚠️ No pude cargar las tareas. Intente de nuevo en un momento."

    if not tasks:
        return (
            "No tiene tareas pendientes.\n\n"
            "✏️ Para agregar una, escriba un punto y la tarea. Ej: \".comprar pan\""
        )

    lines = ["📋 *Tareas pendientes:*"]
    for i, task in enumerate(_sort_tasks(tasks), 1):
        fecha = str(task.get("fecha", "")).strip()
        if fecha:
            lines.append(f"{i}. *{_format_fecha(fecha)}* — {task['tarea']}")
        else:
            lines.append(f"{i}. {task['tarea']}")
    lines.append(
        "\n✏️ *Agregar:* un punto y la tarea → \".comprar pan\"\n"
        "🗑️ *Borrar:* un punto y el número → \".2\""
    )
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
            user, func_args["nombre"], func_args["fecha"], func_args.get("hora"),
            func_args.get("hora_fin"), func_args.get("duracion_min"),
        )
    if func_name == "get_pending_tasks":
        tasks = await get_pending_tasks(user)
        # Return them numbered in the SAME order the user sees (canonical)
        return [
            {"n": i, "tarea": t.get("tarea", ""), "fecha": str(t.get("fecha", "")).strip()}
            for i, t in enumerate(_sort_tasks(tasks), 1)
        ]
    if func_name == "delete_task":
        positions = func_args.get("posiciones") or []
        if not positions and "posicion" in func_args:
            positions = [func_args["posicion"]]
        # Delete from highest to lowest so earlier deletions don't shift the rest
        eliminadas = []
        for p in sorted({int(x) for x in positions}, reverse=True):
            nombre = await delete_task_by_position(user, p)
            if nombre:
                eliminadas.append(nombre)
        return {"ok": len(eliminadas) > 0, "eliminadas": eliminadas}
    if func_name == "add_task":
        task_id = await add_task(user, func_args["tarea"])
        if task_id and func_args.get("fecha"):
            await update_task_fecha(user, task_id, func_args["fecha"])
        return {"ok": task_id is not None, "tarea": func_args["tarea"],
                "fecha": func_args.get("fecha")}
    if func_name == "update_event":
        # Self-contained: find the event by name/date, then update it (one step)
        query = func_args.get("query", "")
        fecha = func_args.get("fecha")
        if fecha:
            events = await get_events_by_date(user, fecha)
            matched = [e for e in events if query.lower() in e.get("nombre", "").lower()] or events
        else:
            matched = await search_event(user, query)
        if not matched:
            return {"ok": False, "error": "No se encontró ningún evento con ese nombre."}
        ev = matched[0]
        result = await update_event(
            user,
            ev["id"],
            nuevo_nombre=func_args.get("nuevo_nombre"),
            nueva_fecha=func_args.get("nueva_fecha"),
            nueva_hora=func_args.get("nueva_hora"),
        )
        return {"ok": True, "evento": result.get("nombre"), "cambios": {
            "nombre": func_args.get("nuevo_nombre"),
            "fecha": func_args.get("nueva_fecha"),
            "hora": func_args.get("nueva_hora"),
        }}
    if func_name == "update_task":
        ok = await update_task(
            user,
            func_args["posicion"],
            nuevo_nombre=func_args.get("nuevo_nombre"),
            nueva_fecha=func_args.get("nueva_fecha"),
        )
        return {"ok": ok}
    if func_name == "add_expense":
        ok = await add_expense(
            user,
            func_args["monto"],
            func_args["categoria"],
            func_args.get("descripcion", ""),
            func_args.get("fecha"),
        )
        return {"ok": ok, "monto": func_args["monto"], "categoria": func_args["categoria"]}
    if func_name == "get_expenses":
        return await get_expenses(
            user,
            desde=func_args.get("desde"),
            hasta=func_args.get("hasta"),
            categoria=func_args.get("categoria"),
        )
    if func_name == "update_expense_monto":
        result = await update_expense_monto(
            user,
            func_args["posicion"],
            func_args["nuevo_monto"],
            desde=func_args.get("desde"),
            hasta=func_args.get("hasta"),
            categoria=func_args.get("categoria"),
        )
        return result or {"error": "No se encontró ese gasto."}
    if func_name == "delete_expense":
        result = await delete_expense(
            user,
            func_args["posicion"],
            desde=func_args.get("desde"),
            hasta=func_args.get("hasta"),
            categoria=func_args.get("categoria"),
        )
        return result or {"error": "No se encontró ese gasto."}
    if func_name == "add_fixed_expense":
        ok = await add_fixed_expense(
            user,
            func_args["nombre"],
            func_args["monto"],
            func_args["categoria"],
            func_args.get("dia_del_mes", 1),
        )
        return {"ok": ok, "nombre": func_args["nombre"], "monto": func_args["monto"]}
    if func_name == "get_fixed_expenses":
        return await get_fixed_expenses(user)
    if func_name == "cancel_fixed_expense":
        nombre = await cancel_fixed_expense(user, func_args["nombre"])
        return {"ok": nombre is not None, "nombre": nombre}
    if func_name == "add_income":
        ok = await add_income(
            user,
            func_args["monto"],
            func_args.get("descripcion", ""),
            func_args.get("fecha"),
        )
        return {"ok": ok, "monto": func_args["monto"]}
    if func_name == "get_balance":
        return await get_balance(
            user, desde=func_args.get("desde"), hasta=func_args.get("hasta")
        )
    if func_name == "add_debt":
        ok = await add_debt(
            user, func_args["persona"], func_args["monto"], func_args.get("tipo", "debo")
        )
        return {"ok": ok}
    if func_name == "get_debts":
        return await get_debts(user)
    if func_name == "settle_debt":
        saldadas = await settle_debt(user, func_args.get("posiciones", []))
        return {"ok": len(saldadas) > 0, "saldadas": saldadas}
    if func_name == "add_super_item":
        ok = await add_super_item(user, func_args["item"])
        return {"ok": ok, "item": func_args["item"]}
    if func_name == "get_super_list":
        return await get_super_list(user)
    if func_name == "remove_super_items":
        quitados = await remove_super_items(user, func_args.get("posiciones", []))
        return {"ok": len(quitados) > 0, "quitados": quitados}
    if func_name == "clear_super_list":
        n = await clear_super_list(user)
        return {"ok": True, "eliminados": n}
    if func_name == "add_to_list":
        n = await add_list_items(user, func_args["nombre"], func_args.get("items", []))
        return {"ok": True, "nombre": func_args["nombre"], "agregados": n}
    if func_name == "get_list":
        items = await get_list_items(user, func_args["nombre"])
        return {"nombre": func_args["nombre"], "items": items}
    if func_name == "get_lists":
        return await get_list_names(user)
    if func_name == "remove_list_items":
        quitados = await remove_list_items(user, func_args["nombre"], func_args.get("posiciones", []))
        return {"ok": len(quitados) > 0, "quitados": quitados}
    if func_name == "delete_list":
        n = await delete_list(user, func_args["nombre"])
        return {"ok": n > 0, "eliminados": n}
    if func_name == "add_reminder":
        fecha = func_args.get("fecha") or datetime.now(ARGENTINA_TZ).strftime("%Y-%m-%d")
        hora = func_args.get("hora", "09:00")
        try:
            dt = datetime.strptime(f"{fecha} {hora}", "%Y-%m-%d %H:%M").replace(tzinfo=ARGENTINA_TZ)
        except ValueError:
            return {"ok": False, "error": "Fecha u hora inválida"}
        if dt <= datetime.now(ARGENTINA_TZ):
            return {"ok": False, "error": "Esa hora ya pasó; pedí una hora futura."}
        ok = await add_reminder(
            user["chat_id"], func_args["texto"], dt.astimezone(timezone.utc).isoformat()
        )
        return {"ok": ok, "cuando": f"{_format_fecha(fecha)} a las {hora}"}
    if func_name == "get_reminders":
        rems = await get_user_reminders(user["chat_id"])
        return [
            {"n": i, "texto": r["texto"], "cuando": _fmt_reminder_when(r["fecha_hora"])}
            for i, r in enumerate(rems, 1)
        ]
    if func_name == "cancel_reminder":
        rems = await get_user_reminders(user["chat_id"])
        canceladas = []
        for p in sorted({int(x) for x in func_args.get("posiciones", [])}, reverse=True):
            if 1 <= p <= len(rems):
                await delete_reminder(rems[p - 1]["id"])
                canceladas.append(rems[p - 1]["texto"])
        return {"ok": len(canceladas) > 0, "canceladas": canceladas}
    return {"error": f"Función desconocida: {func_name}"}


async def _run_tool_calls(msg, messages: list, user: dict, chat_id: int):
    """Execute one assistant message's tool calls and append their results to
    `messages`. Returns (pending_keyboard, show_tasks, direct_reply).
    direct_reply, si no es None, es un texto final armado por el código (ej. la
    confirmación de un evento) que debe usarse tal cual, sin que el modelo lo reformule."""
    pending_keyboard: InlineKeyboardMarkup | None = None
    show_tasks = False
    direct_reply: str | None = None
    event_confirmations: list[str] = []  # una por cada evento creado en esta tanda

    for tc in msg.tool_calls:
        func_name = tc.function.name
        func_args = json.loads(tc.function.arguments)
        logger.info(f"Tool call: {func_name}({func_args}) for user {chat_id}")

        if func_name in ("update_task", "delete_task"):
            show_tasks = True

        if func_name == "add_task":
            tarea = func_args.get("tarea", "")
            fecha = func_args.get("fecha")
            task_id = await add_task(user, tarea)
            if not task_id:
                messages.append({
                    "tool_call_id": tc.id, "role": "tool", "name": func_name,
                    "content": "No se pudo agregar la tarea (configuración de Google incompleta).",
                })
            elif fecha:
                await update_task_fecha(user, task_id, fecha)
                show_tasks = True
                messages.append({
                    "tool_call_id": tc.id, "role": "tool", "name": func_name,
                    "content": json.dumps({"ok": True, "tarea": tarea, "fecha": fecha}, ensure_ascii=False),
                })
            else:
                # No due date given → show the calendar so the user can pick one
                now = datetime.now(ARGENTINA_TZ)
                pending_keyboard = _build_calendar_keyboard(task_id, now.year, now.month)
                messages.append({
                    "tool_call_id": tc.id, "role": "tool", "name": func_name,
                    "content": (
                        f"Tarea '{tarea}' agregada. Decile al usuario que la agregaste y "
                        "preguntale para cuándo es la fecha límite (se le está mostrando un "
                        "calendario para elegirla)."
                    ),
                })
        elif func_name == "delete_event":
            query = func_args.get("query", "")
            fecha = func_args.get("fecha")
            # Search for the event internally
            if fecha:
                events = await get_events_by_date(user, fecha)
                matched = [e for e in events if query.lower() in e.get("nombre", "").lower()]
                if not matched:
                    matched = events  # fallback: show all events for that date
            else:
                matched = await search_event(user, query)

            if not matched:
                messages.append({
                    "tool_call_id": tc.id,
                    "role": "tool",
                    "name": func_name,
                    "content": "No se encontró ningún evento con ese nombre.",
                })
            else:
                ev = matched[0]
                event_id = ev["id"]
                event_name = ev.get("nombre", "evento")
                inicio = ev.get("inicio", "")
                try:
                    dt = datetime.fromisoformat(inicio)
                    time_str = dt.strftime("%d/%m %H:%M")
                    label = f"🗑️ Sí, eliminar — {event_name} ({time_str})"
                except Exception:
                    label = f"🗑️ Sí, eliminar — {event_name}"
                pending_keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton(label, callback_data=f"delEvent_{event_id}"),
                    InlineKeyboardButton("❌ No", callback_data="delEventCancel"),
                ]])
                messages.append({
                    "tool_call_id": tc.id,
                    "role": "tool",
                    "name": func_name,
                    "content": f"Evento encontrado: {event_name}. Mostrando botón de confirmación.",
                })
        elif func_name == "create_event" and func_args.get("hora"):
            nombre = func_args["nombre"]
            fecha = func_args["fecha"]
            hora = func_args["hora"]
            day_events = await get_events_by_date(user, fecha)
            try:
                target_dt = datetime.strptime(f"{fecha} {hora}", "%Y-%m-%d %H:%M").replace(tzinfo=ARGENTINA_TZ)
            except ValueError:
                target_dt = None
            conflicts = (
                [ev for ev in day_events if _within_minutes(ev.get("inicio", ""), target_dt, timedelta(minutes=30))]
                if target_dt else []
            )
            if conflicts:
                _pending_event_creates[chat_id] = {
                    "nombre": nombre, "fecha": fecha, "hora": hora,
                    "hora_fin": func_args.get("hora_fin"),
                    "duracion_min": func_args.get("duracion_min"),
                }
                conflict_names = ", ".join(f'"{ev["nombre"]}"' for ev in conflicts[:3])
                pending_keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Sí, agendar igual", callback_data="createConflict_yes"),
                    InlineKeyboardButton("❌ Cancelar", callback_data="createConflict_no"),
                ]])
                messages.append({
                    "tool_call_id": tc.id, "role": "tool", "name": func_name,
                    "content": f"Advertencia: hay evento(s) a menos de 30 minutos ({conflict_names}). Se le mostró al usuario la opción de agendar igual.",
                })
            else:
                result = await _execute_tool(func_name, func_args, user)
                # La confirmación la arma el código desde el evento real (no el modelo).
                event_confirmations.append(_format_event_confirmation(result))
                messages.append({
                    "tool_call_id": tc.id, "role": "tool", "name": func_name,
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                })
        elif func_name == "create_event":
            # Evento de todo el día (sin hora): también confirmación determinística.
            result = await _execute_tool(func_name, func_args, user)
            event_confirmations.append(_format_event_confirmation(result))
            messages.append({
                "tool_call_id": tc.id, "role": "tool", "name": func_name,
                "content": json.dumps(result, ensure_ascii=False, default=str),
            })
        else:
            result = await _execute_tool(func_name, func_args, user)
            messages.append({
                "tool_call_id": tc.id,
                "role": "tool",
                "name": func_name,
                "content": json.dumps(result, ensure_ascii=False, default=str),
            })

    if event_confirmations:
        direct_reply = "\n\n".join(event_confirmations)

    return pending_keyboard, show_tasks, direct_reply


async def _call_openai(
    user: dict, text: str
) -> tuple[str, InlineKeyboardMarkup | None]:
    now = datetime.now(ARGENTINA_TZ)
    today = now.strftime("%Y-%m-%d")
    dia_semana = DIAS_ES[now.weekday()]
    chat_id = user["chat_id"]

    # NOTA: este system_msg es 100% ESTÁTICO (igual para todos, siempre). Eso permite que
    # OpenAI lo cachee y cobre ese bloque más barato. Lo dinámico (la fecha) va aparte, abajo.
    system_msg = {
        "role": "system",
        "content": (
            "Sos un asistente personal de productividad, formal y cordial pero BREVE. "
            "Ayudás a gestionar tareas y eventos de Google Calendar. "
            "Hablás SIEMPRE de USTED (nunca de vos ni de tú), con respeto, pero SIN floreos: "
            "confirmá lo hecho en UNA frase corta y directa, sin despedidas largas ni fórmulas "
            "recargadas (nada de 'quedo a su entera disposición', 'será un placer', etc.). "
            "NO uses 'señor' ni 'señora' ni ningún término con género para dirigirte a la "
            "persona: mantené un trato formal y neutro (de usted). "
            "El usuario puede escribirte de vos, pero respondé siempre con este trato formal. "
            "Cuando el usuario pide una hora en punto ('a las 4', 'a las 10'), usá HH:00. "
            "Si da minutos exactos ('19:30', 'siete y media'), respetalos TAL CUAL: NUNCA redondees. "
            "\n\nREGLA PARA CREAR EVENTOS (create_event): "
            "Pasá la hora de inicio en 'hora' (HH:MM, 24h) copiando exactamente lo que dijo el "
            "usuario. Si da un rango ('de 19 a 20', 'de 9 a 10:30'), pasá también 'hora_fin'. "
            "Si da una duración ('reunión de 2 horas', 'media hora'), pasá 'duracion_min' en minutos. "
            "Si no da hora exacta pero sí una franja del día, traducila: mañana=09:00, mediodía=13:00, "
            "tarde=16:00, tardecita/al caer la tarde=18:00, noche=20:00, bien de noche=22:00 "
            "(interpolá los casos intermedios, ej. 'tarde-noche'≈19:00). "
            "Si NO hay NINGUNA referencia horaria (ni hora ni franja), NO pases 'hora': se agenda "
            "como evento de todo el día. "
            "No escribas vos la confirmación del evento: el sistema la arma con la hora real y el link."
            "\n\nREGLA OBLIGATORIA PARA ELIMINAR EVENTOS: "
            "Cuando el usuario quiera eliminar un evento, llamá INMEDIATAMENTE delete_event "
            "con el nombre del evento y la fecha. NO busques el evento antes, NO pidas confirmación con texto. "
            "El sistema se encarga de buscar el evento y mostrar el botón de confirmación."
            "\n\nREGLA PARA EDITAR EVENTOS: "
            "Cuando el usuario quiera editar un evento (cambiar hora, nombre o fecha), llamá "
            "update_event DIRECTAMENTE, pasando el nombre del evento en 'query' (y 'fecha' si la "
            "sabés) más los cambios. NO uses search_event antes: update_event ya busca el evento. "
            "Después de llamarlo, confirmá que YA quedó modificado (no digas que lo vas a hacer)."
            "\n\nREGLA PARA RECORDATORIOS CON HORA: "
            "Si el usuario pide que le RECUERDES o AVISES algo a una hora específica "
            "('recordame X mañana a las 3', 'avisame a las 18 que...'), usá add_reminder con el "
            "texto, la fecha (YYYY-MM-DD) y la hora (HH:MM, 24h). Diferencia con tareas: si NO hay "
            "hora, es una tarea (add_task); si hay hora para avisar, es un recordatorio. "
            "Para verlos usá get_reminders y para cancelarlos cancel_reminder por número."
            "\n\nREGLA PARA AGREGAR TAREAS (MUY IMPORTANTE): "
            "Cuando el usuario pida agregar/anotar/sumar/recordar algo que tiene que hacer, "
            "o exprese un pendiente sin hora de calendario (ej. 'comprar pan', 'llamar al médico', "
            "'tengo que ir al banco', 'recordame pagar la luz'), agregalo SIEMPRE con add_task. "
            "NO respondas que lo agregaste sin antes llamar a add_task. "
            "Si menciona una fecha límite, calculala y pasala en formato YYYY-MM-DD. "
            "Si tiene una hora específica (ej. 'a las 10') o es una reunión/cita/turno, "
            "entonces es un EVENTO de calendario (create_event), no una tarea."
            "\n\nREGLA PARA EDITAR/ELIMINAR TAREAS: "
            "Para renombrar o cambiar la fecha de una tarea usá update_task con su número. "
            "Para eliminar una tarea usá delete_task con su número. Los números son los que ve "
            "el usuario; si dudás del número, llamá get_pending_tasks (cada tarea trae su campo 'n'). "
            "Si el usuario pide eliminar varias (ej. 'borrá la 8 y la 14'), llamá delete_task una "
            "vez por cada número."
            "\n\nMUY IMPORTANTE SOBRE LA LISTA DE TAREAS: "
            "NUNCA escribas vos la lista de tareas ni la numeres en tu respuesta; el sistema la "
            "agrega automáticamente al final con el formato y la numeración correctos. "
            "Vos solo confirmá brevemente la acción realizada."
            "\n\nREGLA PARA GASTOS: "
            "Si el usuario dice que YA gastó/pagó/compró algo (verbo en pasado, con un monto), "
            "registralo con add_expense e inferí la categoría, y guardá una descripción breve. "
            "Si pide ver o listar sus gastos, usá get_expenses y mostralos enumerados con su número, "
            "descripción y monto. Para editar el monto de un gasto usá update_expense_monto y para "
            "borrarlo delete_expense, identificándolo por su número en la lista mostrada (pasá los "
            "mismos filtros de categoría/fechas). "
            "OJO: 'pagar el monotributo' o 'tengo que pagar X' es una TAREA futura, no un gasto. "
            "Solo es gasto si ya ocurrió ('pagué', 'gasté', 'compré'). "
            "\n\nREGLA PARA INGRESOS Y BALANCE: "
            "Si el usuario dice que YA cobró/le pagaron/recibió dinero (verbo en pasado, con monto), "
            "registralo con add_income. Si pregunta por su balance, saldo o cuánto le queda, usá "
            "get_balance calculando el período. El balance es ingresos menos gastos."
            "\n\nREGLA PARA DEUDAS: "
            "'le debo X a Y' → add_debt con tipo='debo'. 'Y me debe X' → add_debt con tipo='me_deben'. "
            "Para ver deudas usá get_debts. Cuando pague o le paguen una deuda, usá settle_debt con "
            "su número. NUNCA escribas vos la lista de deudas; el sistema la muestra."
            "\n\nREGLA PARA LISTA DE SUPERMERCADO: "
            "Para agregar productos a la lista del súper/compras usá add_super_item (uno por producto). "
            "Para verla usá get_super_list, para quitar productos remove_super_items por número, y "
            "clear_super_list para vaciarla. Diferenciá: 'comprar pan' sin contexto de súper es una "
            "TAREA; 'agregá pan a la lista del súper' es la lista de compras."
            "\n\nREGLA PARA LISTADOS CON NOMBRE: "
            "Son listas que el usuario nombra (viaje, mudanza, regalos, etc.), distintas del súper "
            "y de las tareas. Para crearlas o agregarles ítems usá add_to_list (extraé el nombre y "
            "los ítems del mensaje). Para ver una usá get_list, para ver todas get_lists, para quitar "
            "ítems remove_list_items por número, y delete_list para borrar el listado completo. "
            "Si te piden armar un listado pero no dan ítems, creá el listado y preguntá qué agregar."
            "\n\nREGLA PARA GASTOS FIJOS: "
            "Un gasto fijo es uno que se repite todos los meses (alquiler, seguro, patente, cuota "
            "de club, Netflix, etc.). Cuando el usuario lo declare ('el alquiler son 200000 por mes'), "
            "registralo con add_fixed_expense. Para verlos usá get_fixed_expenses y para darlos de "
            "baja cancel_fixed_expense. Los gastos fijos se cargan solos como gasto cada mes."
            f"\nCategorías válidas: {', '.join(EXPENSE_CATEGORIES)}."
        ),
    }

    # La fecha va en un mensaje aparte (dinámico) para no romper la caché del system_msg.
    # Es la misma para todos en el día, así que igual se cachea entre usuarios.
    fecha_msg = {
        "role": "system",
        "content": (
            f"Fecha de hoy: {today} ({dia_semana}). Si el usuario menciona días relativos "
            "(mañana, el lunes, el próximo sábado, etc.), calculá la fecha exacta a partir de hoy."
        ),
    }

    # Build messages: system estático + fecha + history + current
    messages = [system_msg, fecha_msg] + _get_history(chat_id) + [{"role": "user", "content": text}]

    # Force specific tools when intent is clear — gpt-4o-mini hallucinates otherwise.
    # Order matters: most specific first (fixed/expense edits before generic delete/edit).
    tool_choice: str | dict
    if _is_pronoun_delete(text):
        # "eliminalo" / "borrá eso" — resolve by what was just discussed
        topic = _last_topic(chat_id)
        if topic == "expense":
            tool_choice = {"type": "function", "function": {"name": "delete_expense"}}
        elif topic == "event":
            tool_choice = {"type": "function", "function": {"name": "delete_event"}}
        else:
            tool_choice = "auto"
    elif _is_super_query_intent(text):
        tool_choice = {"type": "function", "function": {"name": "get_super_list"}}
    elif _is_super_add_intent(text):
        tool_choice = {"type": "function", "function": {"name": "add_super_item"}}
    elif _is_lists_query_intent(text):
        tool_choice = {"type": "function", "function": {"name": "get_lists"}}
    elif _is_list_create_intent(text):
        tool_choice = {"type": "function", "function": {"name": "add_to_list"}}
    elif _is_debt_query_intent(text):
        tool_choice = {"type": "function", "function": {"name": "get_debts"}}
    elif _is_debt_add_intent(text):
        tool_choice = {"type": "function", "function": {"name": "add_debt"}}
    elif _is_reminder_query_intent(text):
        tool_choice = {"type": "function", "function": {"name": "get_reminders"}}
    elif _is_reminder_add_intent(text):
        tool_choice = {"type": "function", "function": {"name": "add_reminder"}}
    elif _is_task_add_intent(text):
        tool_choice = {"type": "function", "function": {"name": "add_task"}}
    elif _is_event_create_intent(text):
        # "required" (no una función fija) obliga a la IA a actuar pero le permite
        # emitir VARIAS create_event en un mismo mensaje (una por evento), así soporta
        # múltiples eventos sin hardcodear el conteo ni parsear nosotros.
        tool_choice = "required"
    elif _is_fixed_query_intent(text):
        tool_choice = {"type": "function", "function": {"name": "get_fixed_expenses"}}
    elif _is_fixed_cancel_intent(text):
        tool_choice = {"type": "function", "function": {"name": "cancel_fixed_expense"}}
    elif _is_fixed_add_intent(text):
        tool_choice = {"type": "function", "function": {"name": "add_fixed_expense"}}
    elif _is_task_delete_intent(text):
        tool_choice = {"type": "function", "function": {"name": "delete_task"}}
    elif _is_expense_delete_intent(text):
        tool_choice = {"type": "function", "function": {"name": "delete_expense"}}
    elif _is_expense_edit_intent(text):
        tool_choice = {"type": "function", "function": {"name": "update_expense_monto"}}
    elif _is_event_delete_intent(text):
        tool_choice = {"type": "function", "function": {"name": "delete_event"}}
    elif _is_task_edit_intent(text):
        tool_choice = {"type": "function", "function": {"name": "update_task"}}
    elif _is_event_edit_intent(text):
        tool_choice = {"type": "function", "function": {"name": "update_event"}}
    elif _is_balance_intent(text):
        tool_choice = {"type": "function", "function": {"name": "get_balance"}}
    elif _is_income_add_intent(text):
        tool_choice = {"type": "function", "function": {"name": "add_income"}}
    elif _is_expense_query_intent(text):
        tool_choice = {"type": "function", "function": {"name": "get_expenses"}}
    elif _is_expense_add_intent(text):
        tool_choice = {"type": "function", "function": {"name": "add_expense"}}
    else:
        tool_choice = "auto"
    logger.info(f"tool_choice={tool_choice!r} for: {text[:60]!r}")

    response = await openai_client.chat.completions.create(
        model=CHAT_MODEL,
        messages=messages,
        tools=OPENAI_TOOLS,
        tool_choice=tool_choice,
    )

    msg = response.choices[0].message

    pending_keyboard: InlineKeyboardMarkup | None = None
    show_tasks = False  # only append the tasks list when the list actually changed
    direct_reply: str | None = None
    rounds = 0

    # Multi-round tool loop: keep the tools available so the model can chain
    # actions in the same turn (e.g. search_event → update_event) instead of
    # promising to do something and never doing it.
    while msg.tool_calls and rounds < MAX_TOOL_ROUNDS:
        rounds += 1
        messages.append(msg)
        pk, st, dr = await _run_tool_calls(msg, messages, user, chat_id)
        if pk is not None:
            pending_keyboard = pk
        show_tasks = show_tasks or st
        if dr is not None:
            direct_reply = dr
        # Si una tool armó la respuesta final (ej. confirmación de evento), la usamos
        # tal cual y cortamos: el modelo no la reformula (no redondea ni inventa).
        if direct_reply is not None:
            _add_to_history(chat_id, text, direct_reply)
            return direct_reply, pending_keyboard, show_tasks
        response = await openai_client.chat.completions.create(
            model=CHAT_MODEL,
            messages=messages,
            tools=OPENAI_TOOLS,
            tool_choice="auto",
        )
        msg = response.choices[0].message

    reply = msg.content or "Listo."
    _add_to_history(chat_id, text, reply)
    return reply, pending_keyboard, show_tasks


async def _transcribe_voice(voice_bytes: bytes) -> str:
    buf = io.BytesIO(voice_bytes)
    buf.name = "audio.ogg"
    result = await openai_client.audio.transcriptions.create(
        model="whisper-1", file=buf, language="es"
    )
    return result.text


async def _interpret_photo(image_bytes: bytes) -> dict:
    """Look at a photo, classify it (gasto/tarea/evento/texto) and extract the relevant fields."""
    today = datetime.now(ARGENTINA_TZ).strftime("%Y-%m-%d")
    b64 = base64.b64encode(image_bytes).decode()
    resp = await openai_client.chat.completions.create(
        model=CHAT_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "Interpretás una foto y decidís qué acción corresponde. Devolvé SOLO un JSON.\n"
                    "Clave 'tipo', uno de:\n"
                    "- 'gasto': ticket, comprobante, factura o recibo de una compra.\n"
                    "- 'tarea': lista o nota (manuscrita o no) de cosas para hacer / pendientes.\n"
                    "- 'evento': invitación, flyer o nota con fecha y/u hora de un evento.\n"
                    "- 'texto': cualquier otra cosa (captura de chat, email, documento).\n\n"
                    "Si tipo='gasto': agregá monto (number, el total), categoria (una de: "
                    f"{', '.join(EXPENSE_CATEGORIES)}), descripcion (string corta), "
                    f"fecha (YYYY-MM-DD; si no se ve, usá {today}).\n"
                    "Si tipo='tarea': agregá tareas = lista de objetos "
                    "{tarea: string, fecha: 'YYYY-MM-DD' o null}.\n"
                    "Si tipo='evento': agregá nombre (string), evento_fecha (YYYY-MM-DD), "
                    "hora ('HH:MM' o null).\n"
                    "Si tipo='texto': agregá texto = un resumen o transcripción breve de la imagen."
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "¿Qué es esta imagen y qué acción corresponde?"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            },
        ],
        response_format={"type": "json_object"},
    )
    return json.loads(resp.choices[0].message.content or "{}")


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
    await query.edit_message_text(
        header + footer, parse_mode="Markdown", reply_markup=_tasks_help_kb()
    )


# ── Menú interactivo ──────────────────────────────────────────────────────────

def _fmt_money(value) -> str:
    """Format a number as Argentine pesos: 15500 -> $15.500"""
    try:
        return f"${int(round(float(value))):,}".replace(",", ".")
    except (ValueError, TypeError):
        return f"${value}"


def _fmt_dia(fecha_str: str) -> str:
    try:
        return datetime.strptime(fecha_str, "%Y-%m-%d").strftime("%d/%m")
    except (ValueError, TypeError):
        return ""


def _fmt_reminder_when(iso_utc: str) -> str:
    """ISO UTC string → 'martes 16 a las 15:00' en hora de Argentina."""
    try:
        dt = datetime.fromisoformat(str(iso_utc).replace("Z", "+00:00")).astimezone(ARGENTINA_TZ)
        return f"{DIAS_ES[dt.weekday()]} {dt.day} a las {dt.strftime('%H:%M')}"
    except Exception:
        return str(iso_utc)


def _build_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💸 Gastos", callback_data="menu_gastos")],
        [InlineKeyboardButton("🔁 Gastos fijos", callback_data="menu_fijos")],
        [InlineKeyboardButton("💰 Balance del mes", callback_data="menu_balance")],
        [InlineKeyboardButton("💳 Deudas", callback_data="menu_deudas")],
        [InlineKeyboardButton("🛒 Lista del súper", callback_data="menu_super")],
        [InlineKeyboardButton("📝 Listados", callback_data="menu_listados")],
        [InlineKeyboardButton("📋 Tareas", callback_data="menu_tareas")],
        [InlineKeyboardButton("📅 Eventos de hoy", callback_data="menu_hoy")],
        [InlineKeyboardButton("📖 Instrucciones", callback_data="menu_help")],
        [InlineKeyboardButton("⚙️ Configuración", callback_data="menu_config")],
        [InlineKeyboardButton("✖️ Cerrar", callback_data="menu_close")],
    ])


def _build_gastos_cat_menu() -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(EXPENSE_CATEGORIES), 2):
        row = []
        for j in (i, i + 1):
            if j < len(EXPENSE_CATEGORIES):
                cat = EXPENSE_CATEGORIES[j]
                emoji = CATEGORIA_EMOJI.get(cat, "")
                row.append(InlineKeyboardButton(f"{emoji} {cat}", callback_data=f"menu_gcat_{j}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Volver", callback_data="menu_main")])
    return InlineKeyboardMarkup(rows)


def _menu_back(target: str, label: str = "⬅️ Volver al menú") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=target)]])


def _tasks_help_kb() -> InlineKeyboardMarkup:
    """Botón al pie de la lista de tareas — por si Sebastian no entiende algo,
    que el usuario pueda abrir el manual y no se frustre."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📖 Manual de uso", callback_data="menu_help_tareas")
    ]])


def _resumen_label(user: dict) -> str:
    hr = user.get("hora_resumen")
    return f"{hr:02d}:00" if isinstance(hr, int) else "Apagado"


def _format_config(user: dict) -> str:
    estado = (user.get("estado_suscripcion") or "—").lower()
    estado_label = {"activo": "Activa ✅", "trial": "Prueba 🎁", "inactivo": "Inactiva ⛔"}.get(
        estado, estado
    )
    venc_line = ""
    venc = user.get("fecha_vencimiento")
    if venc:
        try:
            d = datetime.fromisoformat(str(venc).replace("Z", "+00:00"))
            dias = (d - datetime.now(timezone.utc)).days
            detalle = f" (en {dias} días)" if dias >= 0 else " (vencida)"
            venc_line = f"\n📅 Vence: *{d.strftime('%d/%m/%Y')}*{detalle}"
        except Exception:
            pass
    google = "Conectada ✅" if user.get("access_token") else "No conectada ❌"
    return (
        "⚙️ *Configuración*\n\n"
        f"🌅 Resumen diario: *{_resumen_label(user)}*\n"
        f"🔑 Suscripción: *{estado_label}*{venc_line}\n"
        f"🔗 Cuenta de Google: *{google}*"
    )


def _build_config_menu(user: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🌅 Resumen diario: {_resumen_label(user)}", callback_data="menu_resumen")],
        [InlineKeyboardButton("🔗 Reconectar Google", callback_data="menu_reconnect")],
        [InlineKeyboardButton("⬅️ Volver al menú", callback_data="menu_main")],
    ])


# Horas ofrecidas para el resumen diario.
_RESUMEN_HORAS = [6, 7, 8, 9, 12, 18, 20, 21, 22]


def _build_resumen_menu() -> InlineKeyboardMarkup:
    rows, fila = [], []
    for h in _RESUMEN_HORAS:
        fila.append(InlineKeyboardButton(f"{h:02d}:00", callback_data=f"menu_setres_{h}"))
        if len(fila) == 3:
            rows.append(fila); fila = []
    if fila:
        rows.append(fila)
    rows.append([InlineKeyboardButton("🔕 Apagar resumen", callback_data="menu_setres_off")])
    rows.append([InlineKeyboardButton("⬅️ Volver", callback_data="menu_config")])
    return InlineKeyboardMarkup(rows)


def _format_categoria_gastos(cat: str, data: dict) -> str:
    emoji = CATEGORIA_EMOJI.get(cat, "")
    gastos = data.get("gastos", [])
    if not gastos:
        return f"{emoji} *{cat}* — este mes\n\nNo hay gastos en esta categoría este mes."
    lines = [f"{emoji} *{cat}* — este mes\n"]
    for g in gastos:
        desc = g.get("descripcion") or "(sin descripción)"
        dia = _fmt_dia(str(g.get("fecha", "")))
        suffix = f" ({dia})" if dia else ""
        lines.append(f"{g['n']}. {desc} — {_fmt_money(g.get('monto'))}{suffix}")
    lines.append(f"\n*Total:* {_fmt_money(data.get('total', 0))}")
    return "\n".join(lines)


def _format_fijos(fijos: list[dict]) -> str:
    if not fijos:
        return "🔁 *Gastos fijos*\n\nNo tiene gastos fijos activos."
    lines = ["🔁 *Gastos fijos activos:*\n"]
    for f in fijos:
        emoji = CATEGORIA_EMOJI.get(str(f.get("categoria", "")), "")
        dia = f.get("dia_del_mes", 1)
        lines.append(f"• {emoji} {f.get('nombre')} — {_fmt_money(f.get('monto'))} (día {dia})")
    return "\n".join(lines)


def _format_balance(bal: dict) -> str:
    ingresos = bal.get("ingresos", 0)
    gastos = bal.get("gastos", 0)
    balance = bal.get("balance", 0)
    signo = "🟢" if balance >= 0 else "🔴"
    return (
        "💰 *Balance de este mes*\n\n"
        f"⬆️ Ingresos: {_fmt_money(ingresos)}\n"
        f"⬇️ Gastos: {_fmt_money(gastos)}\n"
        f"{signo} *Neto: {_fmt_money(balance)}*"
    )


def _format_deudas(data: dict) -> str:
    deudas = data.get("deudas", [])
    if not deudas:
        return "💳 *Deudas*\n\nNo tiene deudas registradas."
    lines = ["💳 *Deudas:*\n"]
    for d in deudas:
        if str(d.get("tipo")) == "me_deben":
            lines.append(f"{d['n']}. {d['persona']} le debe — {_fmt_money(d['monto'])}")
        else:
            lines.append(f"{d['n']}. Debe a {d['persona']} — {_fmt_money(d['monto'])}")
    lines.append(
        f"\n🔴 Debe en total: {_fmt_money(data.get('total_debo', 0))}"
        f"\n🟢 Le deben: {_fmt_money(data.get('total_me_deben', 0))}"
    )
    return "\n".join(lines)


def _build_deudas_menu(data: dict) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            f"✅ Saldar {d['n']} — {d['persona']} {_fmt_money(d['monto'])}",
            callback_data=f"menu_deudadel_{d['n']}",
        )]
        for d in data.get("deudas", [])
    ]
    rows.append([InlineKeyboardButton("⬅️ Volver al menú", callback_data="menu_main")])
    return InlineKeyboardMarkup(rows)


def _format_super(items: list[dict]) -> str:
    if not items:
        return "🛒 *Lista del súper*\n\nLa lista está vacía."
    lines = ["🛒 *Lista del súper:*\n"]
    for it in items:
        lines.append(f"{it['n']}. {it['item']}")
    lines.append("\nToque un producto abajo para quitarlo.")
    return "\n".join(lines)


def _build_super_menu(items: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("➕ Agregar productos", callback_data="menu_superadd")]]
    rows += [
        [InlineKeyboardButton(f"🗑️ {it['item']}", callback_data=f"menu_superdel_{it['n']}")]
        for it in items
    ]
    if items:
        rows.append([InlineKeyboardButton("🧹 Vaciar lista", callback_data="menu_superclear")])
    rows.append([InlineKeyboardButton("⬅️ Volver al menú", callback_data="menu_main")])
    return InlineKeyboardMarkup(rows)


def _format_lists(names: list[dict]) -> str:
    if not names:
        return "📝 *Listados*\n\nNo tiene listados todavía.\nDiga: \"armame una lista de viaje con ...\""
    lines = ["📝 *Sus listados:*\n"]
    for x in names:
        lines.append(f"• {x['nombre']} ({x['items']})")
    return "\n".join(lines)


def _build_lists_menu(names: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"📝 {x['nombre']} ({x['items']})", callback_data=f"menu_lopen_{i}")]
        for i, x in enumerate(names)
    ]
    rows.append([InlineKeyboardButton("➕ Nuevo listado", callback_data="menu_listnew")])
    rows.append([InlineKeyboardButton("⬅️ Volver al menú", callback_data="menu_main")])
    return InlineKeyboardMarkup(rows)


def _format_list(nombre: str, items: list[dict]) -> str:
    if not items:
        return f"📝 *{nombre}*\n\nEl listado está vacío."
    lines = [f"📝 *{nombre}:*\n"]
    for it in items:
        lines.append(f"{it['n']}. {it['item']}")
    lines.append("\nToque un ítem para quitarlo, o ➕ para agregar.")
    return "\n".join(lines)


def _build_list_menu(idx: int, items: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("➕ Agregar ítems", callback_data=f"menu_ladd_{idx}")]]
    rows += [
        [InlineKeyboardButton(f"🗑️ {it['item']}", callback_data=f"menu_lidel_{idx}_{it['n']}")]
        for it in items
    ]
    if items:
        rows.append([InlineKeyboardButton("🗑️ Borrar listado", callback_data=f"menu_ldelall_{idx}")])
    rows.append([InlineKeyboardButton("⬅️ Volver a listados", callback_data="menu_listados")])
    return InlineKeyboardMarkup(rows)


def _format_today_events(events: list[dict]) -> str:
    if not events:
        return "📅 No tiene eventos para hoy."
    lines = ["📅 *Eventos de hoy:*\n"]
    for ev in events:
        inicio = ev.get("inicio", "")
        if "T" in inicio:
            hora = inicio.split("T")[1][:5]
            lines.append(f"• {hora} — {ev['nombre']}")
        else:
            lines.append(f"• Todo el día — {ev['nombre']}")
    return "\n".join(lines)


async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = update.effective_chat.id
    user = await get_user(chat_id)
    if not user:
        return

    if data == "menu_close":
        await query.edit_message_text("Menú cerrado.")
        return

    if data == "menu_main":
        await query.edit_message_text(
            "📲 *Menú* — ¿qué desea ver?",
            parse_mode="Markdown", reply_markup=_build_main_menu(),
        )
        return

    if data == "menu_gastos":
        await query.edit_message_text(
            "💸 *Gastos por categoría* — elija una:",
            parse_mode="Markdown", reply_markup=_build_gastos_cat_menu(),
        )
        return

    if data == "menu_tareas":
        footer = await build_tasks_footer(user)
        await query.edit_message_text(
            footer, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📖 Manual de uso", callback_data="menu_help_tareas")],
                [InlineKeyboardButton("⬅️ Volver al menú", callback_data="menu_main")],
            ]),
        )
        return

    if data == "menu_fijos":
        fijos = await get_fixed_expenses(user)
        await query.edit_message_text(
            _format_fijos(fijos), parse_mode="Markdown", reply_markup=_menu_back("menu_main"),
        )
        return

    if data == "menu_balance":
        now = datetime.now(ARGENTINA_TZ)
        desde = now.replace(day=1).strftime("%Y-%m-%d")
        hasta = now.strftime("%Y-%m-%d")
        bal = await get_balance(user, desde=desde, hasta=hasta)
        await query.edit_message_text(
            _format_balance(bal), parse_mode="Markdown", reply_markup=_menu_back("menu_main"),
        )
        return

    if data == "menu_deudas":
        deudas = await get_debts(user)
        await query.edit_message_text(
            _format_deudas(deudas), parse_mode="Markdown", reply_markup=_build_deudas_menu(deudas),
        )
        return

    if data.startswith("menu_deudadel_"):
        n = int(data.rsplit("_", 1)[1])
        await settle_debt(user, [n])
        deudas = await get_debts(user)
        await query.edit_message_text(
            _format_deudas(deudas), parse_mode="Markdown", reply_markup=_build_deudas_menu(deudas),
        )
        return

    if data == "menu_super":
        items = await get_super_list(user)
        await query.edit_message_text(
            _format_super(items), parse_mode="Markdown", reply_markup=_build_super_menu(items),
        )
        return

    if data == "menu_superadd":
        _super_add_mode.add(chat_id)
        await query.message.reply_text(
            "🛒 *Modo lista del súper activado.*\n"
            "Mandame los productos (uno por línea o separados por coma) y los voy agregando. "
            "Puede pegar un listado entero.\n\nEscriba *listo* cuando termine.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Terminar", callback_data="menu_superdone")
            ]]),
        )
        return

    if data == "menu_superdone":
        _super_add_mode.discard(chat_id)
        items = await get_super_list(user)
        await query.edit_message_text(
            "✅ Listo.\n\n" + _format_super(items), parse_mode="Markdown",
            reply_markup=_build_super_menu(items),
        )
        return

    if data.startswith("menu_superdel_"):
        n = int(data.rsplit("_", 1)[1])
        await remove_super_items(user, [n])
        items = await get_super_list(user)
        await query.edit_message_text(
            _format_super(items), parse_mode="Markdown", reply_markup=_build_super_menu(items),
        )
        return

    if data == "menu_superclear":
        await clear_super_list(user)
        items = await get_super_list(user)
        await query.edit_message_text(
            _format_super(items), parse_mode="Markdown", reply_markup=_build_super_menu(items),
        )
        return

    if data == "menu_listados":
        names = await get_list_names(user)
        await query.edit_message_text(
            _format_lists(names), parse_mode="Markdown", reply_markup=_build_lists_menu(names),
        )
        return

    if data == "menu_listnew":
        _awaiting_list_name.add(chat_id)
        await query.message.reply_text(
            "📝 ¿Cómo se va a llamar el listado? Mándeme el nombre (ej. *Viaje*).",
            parse_mode="Markdown",
        )
        return

    if data.startswith("menu_lopen_"):
        idx = int(data.rsplit("_", 1)[1])
        names = await get_list_names(user)
        if idx >= len(names):
            return
        nombre = names[idx]["nombre"]
        items = await get_list_items(user, nombre)
        await query.edit_message_text(
            _format_list(nombre, items), parse_mode="Markdown",
            reply_markup=_build_list_menu(idx, items),
        )
        return

    if data.startswith("menu_ladd_"):
        idx = int(data.rsplit("_", 1)[1])
        names = await get_list_names(user)
        if idx >= len(names):
            return
        nombre = names[idx]["nombre"]
        _list_add_mode[chat_id] = nombre
        await query.message.reply_text(
            f"📝 *Modo listado «{nombre}».*\nMándeme los ítems (uno por línea o con comas). "
            "Puede pegar un listado entero.\n\nEscriba *listo* cuando termine.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Terminar", callback_data="menu_listdone")
            ]]),
        )
        return

    if data.startswith("menu_lidel_"):
        _, _, idx_s, n_s = data.split("_", 3)
        names = await get_list_names(user)
        if int(idx_s) >= len(names):
            return
        nombre = names[int(idx_s)]["nombre"]
        await remove_list_items(user, nombre, [int(n_s)])
        items = await get_list_items(user, nombre)
        await query.edit_message_text(
            _format_list(nombre, items), parse_mode="Markdown",
            reply_markup=_build_list_menu(int(idx_s), items),
        )
        return

    if data.startswith("menu_ldelall_"):
        idx = int(data.rsplit("_", 1)[1])
        names = await get_list_names(user)
        if idx < len(names):
            await delete_list(user, names[idx]["nombre"])
        names = await get_list_names(user)
        await query.edit_message_text(
            _format_lists(names), parse_mode="Markdown", reply_markup=_build_lists_menu(names),
        )
        return

    if data == "menu_listdone":
        _list_add_mode.pop(chat_id, None)
        names = await get_list_names(user)
        await query.edit_message_text(
            "✅ Listo.\n\n" + _format_lists(names), parse_mode="Markdown",
            reply_markup=_build_lists_menu(names),
        )
        return

    if data == "menu_hoy":
        events = await get_today_events(user)
        await query.edit_message_text(
            _format_today_events(events), parse_mode="Markdown",
            reply_markup=_menu_back("menu_main"),
        )
        return


    if data == "menu_help_tareas":
        await query.edit_message_text(
            MANUAL_TAREAS, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📖 Instrucciones de todas las funciones", callback_data="menu_help")],
                [InlineKeyboardButton("⬅️ Volver al menú", callback_data="menu_main")],
            ]),
        )
        return

    if data == "menu_help":
        await query.edit_message_text(
            INSTRUCCIONES_TEXTO, parse_mode="Markdown",
            reply_markup=_menu_back("menu_main"),
        )
        return

    if data == "menu_config":
        await query.edit_message_text(
            _format_config(user), parse_mode="Markdown",
            reply_markup=_build_config_menu(user),
        )
        return

    if data == "menu_reconnect":
        oauth_url = await _make_oauth_url(chat_id)
        await query.edit_message_text(
            "🔗 Para reconectar su cuenta de Google, abra este enlace "
            f"(sus datos no se borran):\n{oauth_url}",
            reply_markup=_menu_back("menu_config"),
        )
        return

    if data == "menu_resumen":
        await query.edit_message_text(
            "🌅 *Resumen diario*\n\n¿A qué hora quiere recibir el resumen del día "
            "(eventos + tareas)? O apáguelo si prefiere no recibirlo.",
            parse_mode="Markdown", reply_markup=_build_resumen_menu(),
        )
        return

    if data.startswith("menu_setres_"):
        val = data.rsplit("_", 1)[1]
        hora = None if val == "off" else int(val)
        await update_user_resumen(chat_id, hora)
        user = await get_user(chat_id)
        await query.edit_message_text(
            _format_config(user), parse_mode="Markdown",
            reply_markup=_build_config_menu(user),
        )
        return

    if data.startswith("menu_gcat_"):
        idx = int(data.rsplit("_", 1)[1])
        if idx < 0 or idx >= len(EXPENSE_CATEGORIES):
            return
        cat = EXPENSE_CATEGORIES[idx]
        now = datetime.now(ARGENTINA_TZ)
        desde = now.replace(day=1).strftime("%Y-%m-%d")
        hasta = now.strftime("%Y-%m-%d")
        exp = await get_expenses(user, desde=desde, hasta=hasta, categoria=cat)
        await query.edit_message_text(
            _format_categoria_gastos(cat, exp), parse_mode="Markdown",
            reply_markup=_menu_back("menu_gastos", "⬅️ Volver a categorías"),
        )
        return


# ── Message routing ───────────────────────────────────────────────────────────

async def _route_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user: dict, text: str
) -> None:
    message = update.message
    chat_id = user["chat_id"]

    # Waiting for the name of a new list (from the menu)
    if chat_id in _awaiting_list_name:
        _awaiting_list_name.discard(chat_id)
        nombre = text.strip()[:60]
        if not nombre or nombre.startswith("."):
            await message.reply_text("Nombre no válido. Probá de nuevo desde el menú.")
            return
        _list_add_mode[chat_id] = nombre
        await message.reply_text(
            f"📝 *Listado «{nombre}» creado.*\nMándeme los ítems (uno por línea o con comas). "
            "Escriba *listo* cuando termine.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Terminar", callback_data="menu_listdone")
            ]]),
        )
        return

    # Named-list "add mode" — everything sent goes into that list until "listo"
    if chat_id in _list_add_mode:
        nombre = _list_add_mode[chat_id]
        if text.strip().lower() in (
            "listo", "salir", "menu", "menú", "chau", "fin", "terminar", "ya está", "ya esta", "."
        ):
            _list_add_mode.pop(chat_id, None)
            items = await get_list_items(user, nombre)
            await message.reply_text(
                "✅ Listo.\n\n" + _format_list(nombre, items), parse_mode="Markdown",
            )
            return
        nuevos = _split_items(text)
        await add_list_items(user, nombre, nuevos)
        added = "\n".join(f"• {n}" for n in nuevos) or "(nada)"
        await message.reply_text(
            f"✅ Agregué {len(nuevos)} a «{nombre}»:\n{added}\n\n"
            "Seguí mandando más o escribí *listo* para terminar.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Terminar", callback_data="menu_listdone")
            ]]),
        )
        return

    # Supermarket "add mode" — everything sent is treated as items until "listo"
    if chat_id in _super_add_mode:
        if text.strip().lower() in (
            "listo", "salir", "menu", "menú", "chau", "fin", "terminar", "ya está", "ya esta", "."
        ):
            _super_add_mode.discard(chat_id)
            items = await get_super_list(user)
            await message.reply_text(
                "✅ Listo.\n\n" + _format_super(items),
                parse_mode="Markdown", reply_markup=_build_super_menu(items),
            )
            return
        nuevos = _split_items(text)
        for it in nuevos:
            await add_super_item(user, it)
        added = "\n".join(f"• {n}" for n in nuevos) or "(nada)"
        await message.reply_text(
            f"✅ Agregué {len(nuevos)} a la lista del súper:\n{added}\n\n"
            "Seguí mandando más o escribí *listo* para terminar.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Terminar", callback_data="menu_superdone")
            ]]),
        )
        return

    if text.startswith("."):
        content = text[1:].strip()

        if content.lower() in ("tareas", "lista"):
            footer = await build_tasks_footer(user)
            await message.reply_text(footer, parse_mode="Markdown", reply_markup=_tasks_help_kb())
            return

        if re.match(r"^\d+$", content):
            pos = int(content)
            deleted_name = await delete_task_by_position(user, pos)
            prefix = (
                f"✅ Eliminada: *{deleted_name}*\n\n"
                if deleted_name is not None
                else f"⚠️ No encontré la tarea n.° {pos}.\n\n"
            )
            footer = await build_tasks_footer(user)
            await message.reply_text(
                prefix + footer, parse_mode="Markdown", reply_markup=_tasks_help_kb()
            )

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
                    "Complete primero la configuración de Google."
                )
        else:
            footer = await build_tasks_footer(user)
            await message.reply_text(
                "⚠️ Para agregar, un punto y la tarea (\".comprar pan\"). "
                "Para borrar, un punto y el número (\".2\").\n\n" + footer,
                parse_mode="Markdown", reply_markup=_tasks_help_kb(),
            )
        return

    # Menú interactivo
    if _is_menu_request(text):
        await message.reply_text(
            "📲 *Menú* — ¿qué desea ver?",
            parse_mode="Markdown",
            reply_markup=_build_main_menu(),
        )
        return

    # Pedido explícito de la lista de tareas
    if _is_task_list_request(text):
        footer = await build_tasks_footer(user)
        await message.reply_text(footer, parse_mode="Markdown", reply_markup=_tasks_help_kb())
        return

    # Pedido de la lista del súper → mostrarla con botones (incluye "➕ Agregar")
    if _is_super_query_intent(text):
        items = await get_super_list(user)
        await message.reply_text(
            _format_super(items), parse_mode="Markdown", reply_markup=_build_super_menu(items),
        )
        return

    # Pedido de deudas → mostrarlas con botones para saldar
    if _is_debt_query_intent(text):
        deudas = await get_debts(user)
        await message.reply_text(
            _format_deudas(deudas), parse_mode="Markdown", reply_markup=_build_deudas_menu(deudas),
        )
        return

    # OpenAI function calling
    show_tasks = False
    try:
        reply, keyboard, show_tasks = await _call_openai(user, text)
    except GoogleAuthExpiredError:
        await message.reply_text(await _session_expired_text(user["chat_id"]))
        return
    except Exception as e:
        logger.error(f"OpenAI error for user {user['chat_id']}: {e}")
        reply, keyboard = "⚠️ Tuve un error procesando su mensaje. Intente de nuevo.", None

    # The tasks list is only appended when the list actually changed.
    footer = await build_tasks_footer(user) if show_tasks else None
    full_text = reply + ("\n\n" + footer if footer else "")
    # When the full list rides along and the action didn't attach its own
    # keyboard, add the manual button so it shows with every complete list.
    footer_kb = _tasks_help_kb() if (footer and keyboard is None) else None
    try:
        await message.reply_text(
            full_text, parse_mode="Markdown", reply_markup=keyboard or footer_kb
        )
    except Exception:
        # Reply may contain special chars — send plain, then footer with Markdown
        await message.reply_text(reply, reply_markup=keyboard)
        if footer:
            await message.reply_text(footer, parse_mode="Markdown", reply_markup=footer_kb)


# ── Create event conflict confirmation callback ───────────────────────────────

async def handle_create_conflict(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id

    if query.data == "createConflict_no":
        _pending_event_creates.pop(chat_id, None)
        await query.edit_message_text("❌ Evento no agendado.")
        return

    pending = _pending_event_creates.pop(chat_id, None)
    if not pending:
        await query.edit_message_text("⚠️ No había ningún evento pendiente.")
        return

    user = await get_user(chat_id)
    if not user:
        return

    try:
        result = await create_event(
            user, pending["nombre"], pending["fecha"], pending["hora"],
            pending.get("hora_fin"), pending.get("duracion_min"),
        )
        await query.edit_message_text(
            _format_event_confirmation(result), parse_mode="Markdown",
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error(f"Error creating event after conflict confirm: {e}")
        await query.edit_message_text("⚠️ No se pudo crear el evento.")


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


async def _session_expired_text(chat_id: int) -> str:
    oauth_url = await _make_oauth_url(chat_id)
    return (
        "⚠️ Su sesión de Google expiró. Reconecte su cuenta (sus datos no se borran):\n\n"
        f"{oauth_url}"
    )


# ── /broadcast command (admin only) ──────────────────────────────────────────

ADMIN_CHAT_ID = int(os.getenv("DAILY_SUMMARY_CHAT_ID", "0"))


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if chat_id != ADMIN_CHAT_ID:
        return

    # Use the raw text (minus the command) so newlines/formatting survive —
    # context.args collapses everything into single spaces.
    raw = update.message.text or ""
    text = re.sub(r"^/broadcast(@\w+)?\s*", "", raw, count=1).strip()
    if not text:
        await update.message.reply_text("Use: /broadcast <mensaje>")
        return

    users = await get_active_users()
    sent, failed = 0, 0
    for u in users:
        try:
            await context.bot.send_message(chat_id=u["chat_id"], text=text)
            sent += 1
        except Exception:
            failed += 1

    await update.message.reply_text(f"✅ Enviado a {sent} usuarios. Fallidos: {failed}.")


async def _photo_gasto(message, user: dict, data: dict) -> None:
    try:
        monto = float(data.get("monto") or 0)
    except (ValueError, TypeError):
        monto = 0
    if not monto:
        await message.reply_text(
            "📷 No pude leer el monto del ticket. ¿Me lo dice? "
            "Por ejemplo: \"gasté 5000 en el súper\"."
        )
        return

    categoria = data.get("categoria") or "Otros"
    if categoria not in EXPENSE_CATEGORIES:
        categoria = "Otros"
    descripcion = (data.get("descripcion") or "ticket").strip()
    ok = await add_expense(user, monto, categoria, descripcion, data.get("fecha"))
    if not ok:
        await message.reply_text("⚠️ Leí el ticket pero no pude guardar el gasto. Intente de nuevo.")
        return

    emoji = CATEGORIA_EMOJI.get(categoria, "")
    texto = (
        "✅ Gasto registrado desde el ticket:\n"
        f"{emoji} *{categoria}* — {_fmt_money(monto)}\n"
        f"_{descripcion}_"
    )
    try:
        await message.reply_text(texto, parse_mode="Markdown")
    except Exception:
        await message.reply_text(f"✅ Gasto registrado: {categoria} — {_fmt_money(monto)} ({descripcion})")


async def _photo_tareas(message, user: dict, data: dict) -> None:
    items = data.get("tareas") or []
    added = []
    for it in items:
        tarea = str(it.get("tarea", "")).strip()
        if not tarea:
            continue
        task_id = await add_task(user, tarea)
        if task_id and it.get("fecha"):
            await update_task_fecha(user, task_id, it["fecha"])
        if task_id:
            added.append(tarea)
    if not added:
        await message.reply_text("📷 Vi una nota pero no pude identificar tareas. ¿Me las dice?")
        return
    lines = "\n".join(f"• {a}" for a in added)
    footer = await build_tasks_footer(user)
    await message.reply_text(
        f"✅ Agregué {len(added)} tarea(s) desde la foto:\n{lines}\n\n{footer}",
        parse_mode="Markdown", reply_markup=_tasks_help_kb(),
    )


async def _photo_evento(message, user: dict, data: dict) -> None:
    nombre = data.get("nombre") or "Evento"
    fecha = data.get("evento_fecha")
    hora = data.get("hora")
    if not fecha:
        await message.reply_text("📷 Vi un evento pero no pude leer la fecha. ¿Me la dice?")
        return
    try:
        result = await create_event(user, nombre, fecha, hora)
    except Exception as e:
        logger.error(f"Photo event create error for {user['chat_id']}: {e}")
        await message.reply_text("⚠️ No pude crear el evento desde la foto.")
        return
    confirm = _format_event_confirmation(result)
    try:
        await message.reply_text(confirm, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception:
        await message.reply_text(confirm)


async def _handle_photo(update: Update, user: dict) -> None:
    message = update.message
    try:
        photo = message.photo[-1]  # highest resolution
        photo_file = await photo.get_file()
        raw = await photo_file.download_as_bytearray()
        data = await _interpret_photo(bytes(raw))
    except Exception as e:
        logger.error(f"Photo error for user {user['chat_id']}: {e}")
        await message.reply_text("⚠️ No pude procesar la foto. Intente de nuevo.")
        return

    tipo = str(data.get("tipo", "")).lower()
    if tipo == "gasto":
        await _photo_gasto(message, user, data)
    elif tipo == "tarea":
        await _photo_tareas(message, user, data)
    elif tipo == "evento":
        await _photo_evento(message, user, data)
    else:
        texto = str(data.get("texto", "")).strip()
        if texto:
            cuerpo = f"📷 Esto dice la imagen:\n\n{texto}\n\n¿Desea que haga algo con esto?"
            try:
                await message.reply_text(cuerpo)
            except Exception:
                await message.reply_text("📷 Leí la imagen. ¿Qué desea que haga con esto?")
        else:
            await message.reply_text("📷 No pude interpretar la imagen. ¿Me dice qué es?")


# ── Main handlers ─────────────────────────────────────────────────────────────

async def _make_oauth_url(chat_id: int) -> str:
    """Genera un token opaco atado al chat_id y arma el link de OAuth con él.
    El chat_id ya no viaja por la URL (se resuelve server-side)."""
    token = await create_oauth_flow(chat_id)
    return f"{BASE_URL}/oauth/start?token={token}"


async def _try_activate_code(update: Update, chat_id: int, nombre: str, code: str) -> None:
    code = code.strip().upper()
    ok = await use_activation_code(code, chat_id)
    if not ok:
        await update.message.reply_text("❌ Código inválido o ya utilizado.")
        return
    await activate_user(chat_id, nombre)
    oauth_url = await _make_oauth_url(chat_id)
    await update.message.reply_text(
        "✅ ¡Código activado! Tu suscripción quedó activa por 30 días.\n\n"
        f"Ahora conectá tu cuenta de Google para empezar:\n{oauth_url}"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message:
        return

    chat_id = update.effective_chat.id
    nombre = update.effective_user.first_name or "Usuario"
    user = await get_user(chat_id)
    text_in = (message.text or "").strip()

    # Usuario nuevo (sin registro) → código o suscripción
    if user is None:
        if _looks_like_code(text_in):
            await _try_activate_code(update, chat_id, nombre, text_in)
        else:
            await message.reply_text(WELCOME_NUEVO)
        return

    # Suscripción no activa (inactivo) → puede ingresar un código para reactivar
    if user.get("estado_suscripcion") not in ("activo", "trial"):
        if _looks_like_code(text_in):
            await _try_activate_code(update, chat_id, nombre, text_in)
        else:
            await message.reply_text(INACTIVO_MSG)
        return

    if not user.get("access_token"):
        oauth_url = await _make_oauth_url(chat_id)
        await message.reply_text(
            f"⚠️ Todavía no conectó su cuenta de Google.\n\n"
            f"Complete la configuración aquí:\n{oauth_url}"
        )
        return

    await message.reply_chat_action("typing")

    # Foto → la IA decide qué es (gasto, tarea, evento o texto) y actúa
    if message.photo:
        await _handle_photo(update, user)
        return

    if message.voice:
        try:
            voice_file = await message.voice.get_file()
            raw = await voice_file.download_as_bytearray()
            text = await _transcribe_voice(bytes(raw))
            await message.reply_text(f"🗣️ Transcripción: {text}")
        except Exception as e:
            logger.error(f"Voice transcription error for user {chat_id}: {e}")
            await message.reply_text("⚠️ No pude transcribir el audio. Intente de nuevo.")
            return
    else:
        text = message.text or ""

    if not text.strip():
        return

    await _route_text(update, context, user, text)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = await get_user(chat_id)
    activo = bool(user) and user.get("estado_suscripcion") in ("activo", "trial")

    if activo and user.get("access_token"):
        await update.message.reply_text(
            "👋 ¡Hola! Ya está configurado y listo.\n\n" + INSTRUCCIONES_TEXTO,
            parse_mode="Markdown",
        )
    elif activo:
        oauth_url = await _make_oauth_url(chat_id)
        await update.message.reply_text(
            "✅ Tu suscripción está activa.\n\n"
            f"Conectá tu cuenta de Google para empezar:\n{oauth_url}"
        )
    else:
        await update.message.reply_text(WELCOME_NUEVO)


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = await get_user(chat_id)
    if not user or not user.get("access_token"):
        await update.message.reply_text("⚠️ Primero necesita conectar su cuenta de Google.")
        return
    await update.message.reply_text(
        "📲 *Menú* — ¿qué desea ver?",
        parse_mode="Markdown", reply_markup=_build_main_menu(),
    )


async def reconectar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = await get_user(chat_id)
    # Solo para usuarios con suscripción activa/trial (no es una vía para saltear el pago)
    if not user or user.get("estado_suscripcion") not in ("activo", "trial"):
        await update.message.reply_text(WELCOME_NUEVO)
        return
    oauth_url = await _make_oauth_url(chat_id)
    await update.message.reply_text(
        f"🔗 Haga clic aquí para reconectar su cuenta de Google:\n{oauth_url}\n\n"
        "Sus tareas y datos no se borrarán."
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
    app.add_handler(CommandHandler("reconectar", reconectar_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(MessageHandler(filters.TEXT | filters.VOICE | filters.PHOTO, handle_message))
    app.add_handler(CallbackQueryHandler(handle_cal_nav, pattern=r"^calNav_|^calIgnore$"))
    app.add_handler(CallbackQueryHandler(handle_cal_day, pattern=r"^calDay_"))
    app.add_handler(CallbackQueryHandler(handle_delete_event, pattern=r"^delEvent"))
    app.add_handler(CallbackQueryHandler(handle_create_conflict, pattern=r"^createConflict_"))
    app.add_handler(CallbackQueryHandler(handle_menu, pattern=r"^menu_"))

    logger.info("Bot starting — polling for updates")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
