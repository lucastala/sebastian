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
    add_email_watch,
    create_user,
    get_active_users,
    get_email_watches,
    get_user,
    remove_email_watch,
)
from google_services import (
    GmailPermissionError,
    add_expense,
    add_fixed_expense,
    add_task,
    cancel_fixed_expense,
    create_event,
    delete_event,
    delete_expense,
    delete_task_by_position,
    get_events_by_date,
    get_expenses,
    get_fixed_expenses,
    get_pending_tasks,
    get_today_events,
    search_emails,
    search_event,
    send_email,
    update_event,
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

PAYMENT_LINK = os.getenv("PAYMENT_LINK", "https://tu-link-de-pago.com")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
ARGENTINA_TZ = timezone(timedelta(hours=-3))

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
_WATCH_EMAIL_PHRASES = (
    "avisame cuando", "avísame cuando", "avisame si", "avísame si",
    "avisame cada vez", "avísame cada vez",
    "notificame cuando", "notifícame cuando", "notificame si", "notifícame si",
    "notificame cada vez", "notifícame cada vez",
    "vigilá los mails", "vigila los mails", "vigilá el mail", "vigila el mail",
    "vigilá los correos", "vigila los correos",
    "cuando me llegue un mail", "cuando llegue un mail",
    "cuando me escriba", "si me escribe",
)
_UNWATCH_EMAIL_PHRASES = (
    "dejá de vigilar", "deja de vigilar",
    "ya no me avises", "dejá de avisarme",
    "sacá la vigilancia", "saca la vigilancia",
    "stop vigilar",
)
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
    "anotá", "anota", "anotame", "anotame", "sumá", "sumame", "agregar",
    "agendame la tarea", "poné en la lista", "pone en la lista", "ponme en la lista",
)
_TASK_CONTEXT = ("tarea", "tareas", "lista", "pendiente", "pendientes")
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


def _is_event_edit_intent(text: str) -> bool:
    t = text.lower()
    return any(p in t for p in _EDIT_EVENT_STEMS)


def _is_watch_email_intent(text: str) -> bool:
    t = text.lower()
    if any(p in t for p in _WATCH_EMAIL_PHRASES):
        return True
    has_notify = any(w in t for w in ("avisame", "avísame", "notificame", "notifícame"))
    has_mail = any(w in t for w in ("mail", "correo", "email"))
    # dirección de email presente (tiene @)
    has_at = "@" in t
    return has_notify and (has_mail or has_at)


def _is_unwatch_email_intent(text: str) -> bool:
    t = text.lower()
    return any(p in t for p in _UNWATCH_EMAIL_PHRASES)


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
    has_verb = any(v in t for v in _TASK_ADD_VERBS)
    has_ctx = any(c in t for c in _TASK_CONTEXT)
    return has_verb and has_ctx


def _is_expense_delete_intent(text: str) -> bool:
    t = text.lower()
    if "fijo" in t:
        return False
    has_verb = any(v in t for v in _EXPENSE_DELETE_VERBS)
    return has_verb and "gasto" in t and bool(re.search(r"\d", t))


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
                "Edita un evento existente del Google Calendar. "
                "Primero buscá el evento con search_event o get_events_by_date para obtener su ID. "
                "Podés cambiar el nombre, la fecha y/o la hora."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "string",
                        "description": "ID del evento a editar",
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
                "required": ["event_id"],
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
            "name": "search_emails",
            "description": "Busca emails en Gmail por remitente, asunto o palabras clave.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Búsqueda de Gmail. Ej: 'from:banco@example.com', 'factura', 'subject:reunión'",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_email",
            "description": "Redacta y envía un email desde la cuenta Gmail del usuario.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Dirección de destino"},
                    "subject": {"type": "string", "description": "Asunto del email"},
                    "body": {"type": "string", "description": "Cuerpo del email"},
                },
                "required": ["to", "subject", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "watch_email",
            "description": (
                "Activa la vigilancia de una dirección de email: cuando llegue un mail de esa "
                "dirección se le notifica al usuario automáticamente. "
                "Usá esta función cuando el usuario pida 'avisame cuando me llegue un mail de X', "
                "'vigilá los mails de X', 'notificame si X me escribe', etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "email_address": {
                        "type": "string",
                        "description": "Dirección de email a vigilar",
                    },
                },
                "required": ["email_address"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unwatch_email",
            "description": (
                "Desactiva la vigilancia de una dirección de email. "
                "Usá esta función cuando el usuario pida 'dejá de vigilar X', "
                "'ya no me avises de X', 'sacá la vigilancia de X', etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "email_address": {
                        "type": "string",
                        "description": "Dirección de email que se deja de vigilar",
                    },
                },
                "required": ["email_address"],
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
        return "No tiene tareas pendientes.\n\nUse .texto para agregar una tarea."

    lines = ["📋 *Tareas pendientes:*"]
    for i, task in enumerate(_sort_tasks(tasks), 1):
        fecha = str(task.get("fecha", "")).strip()
        if fecha:
            lines.append(f"{i}. *{_format_fecha(fecha)}* — {task['tarea']}")
        else:
            lines.append(f"{i}. {task['tarea']}")
    lines.append("\nUse .texto para agregar una tarea. Use .número para eliminar.")
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
    if func_name == "add_task":
        task_id = await add_task(user, func_args["tarea"])
        if task_id and func_args.get("fecha"):
            await update_task_fecha(user, task_id, func_args["fecha"])
        return {"ok": task_id is not None, "tarea": func_args["tarea"],
                "fecha": func_args.get("fecha")}
    if func_name == "update_event":
        result = await update_event(
            user,
            func_args["event_id"],
            nuevo_nombre=func_args.get("nuevo_nombre"),
            nueva_fecha=func_args.get("nueva_fecha"),
            nueva_hora=func_args.get("nueva_hora"),
        )
        return result
    if func_name == "update_task":
        ok = await update_task(
            user,
            func_args["posicion"],
            nuevo_nombre=func_args.get("nuevo_nombre"),
            nueva_fecha=func_args.get("nueva_fecha"),
        )
        return {"ok": ok}
    if func_name == "search_emails":
        return await search_emails(user, func_args["query"])
    if func_name == "send_email":
        ok = await send_email(user, func_args["to"], func_args["subject"], func_args["body"])
        return {"ok": ok}
    if func_name == "watch_email":
        ok = await add_email_watch(user["chat_id"], func_args["email_address"])
        return {"ok": ok, "email": func_args["email_address"]}
    if func_name == "unwatch_email":
        ok = await remove_email_watch(user["chat_id"], func_args["email_address"])
        return {"ok": ok, "email": func_args["email_address"]}
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
    return {"error": f"Función desconocida: {func_name}"}


async def _call_openai(
    user: dict, text: str
) -> tuple[str, InlineKeyboardMarkup | None]:
    now = datetime.now(ARGENTINA_TZ)
    today = now.strftime("%Y-%m-%d")
    dia_semana = DIAS_ES[now.weekday()]
    chat_id = user["chat_id"]

    system_msg = {
        "role": "system",
        "content": (
            "Sos un asistente personal de productividad. "
            "Ayudás a gestionar tareas y eventos de Google Calendar. "
            "Dirigite al usuario SIEMPRE de USTED (nunca de vos ni de tú): "
            "usá 'usted', 'tiene', 'puede', 'avíseme', 'su cuenta', etc. "
            "Mantené un tono cordial y profesional, claro y conciso, sin ser acartonado. "
            "El usuario puede escribirte de vos, pero vos siempre respondé de usted. "
            f"La fecha de hoy es {today} ({dia_semana}). "
            "Si el usuario menciona días relativos (mañana, el lunes, el próximo sábado, etc.), "
            "calculá la fecha exacta a partir de hoy usando el día de la semana indicado. "
            "Cuando el usuario pide una hora en punto ('a las 4', 'a las 10'), "
            "usá siempre HH:00 como minutos. "
            "\n\nREGLA OBLIGATORIA PARA ELIMINAR EVENTOS: "
            "Cuando el usuario quiera eliminar un evento, llamá INMEDIATAMENTE delete_event "
            "con el nombre del evento y la fecha. NO busques el evento antes, NO pidas confirmación con texto. "
            "El sistema se encarga de buscar el evento y mostrar el botón de confirmación."
            "\n\nREGLA PARA EDITAR EVENTOS: "
            "Cuando el usuario quiera editar un evento (cambiar hora, nombre o fecha), "
            "primero buscalo con search_event o get_events_by_date para obtener su ID, "
            "luego llamá update_event con los cambios. "
            "\n\nREGLA PARA AGREGAR TAREAS: "
            "Cuando el usuario pida agregar/anotar/sumar una tarea o pendiente, usá add_task. "
            "Si menciona una fecha límite, calculala y pasala en formato YYYY-MM-DD."
            "\n\nREGLA PARA EDITAR TAREAS: "
            "Cuando el usuario quiera renombrar o cambiar la fecha de una tarea, "
            "usá update_task con su número de posición en la lista."
            "\n\nREGLA PARA GASTOS: "
            "Si el usuario dice que YA gastó/pagó/compró algo (verbo en pasado, con un monto), "
            "registralo con add_expense e inferí la categoría, y guardá una descripción breve. "
            "Si pide ver o listar sus gastos, usá get_expenses y mostralos enumerados con su número, "
            "descripción y monto. Para editar el monto de un gasto usá update_expense_monto y para "
            "borrarlo delete_expense, identificándolo por su número en la lista mostrada (pasá los "
            "mismos filtros de categoría/fechas). "
            "OJO: 'pagar el monotributo' o 'tengo que pagar X' es una TAREA futura, no un gasto. "
            "Solo es gasto si ya ocurrió ('pagué', 'gasté', 'compré'). "
            "\n\nREGLA PARA GASTOS FIJOS: "
            "Un gasto fijo es uno que se repite todos los meses (alquiler, seguro, patente, cuota "
            "de club, Netflix, etc.). Cuando el usuario lo declare ('el alquiler son 200000 por mes'), "
            "registralo con add_fixed_expense. Para verlos usá get_fixed_expenses y para darlos de "
            "baja cancel_fixed_expense. Los gastos fijos se cargan solos como gasto cada mes."
            f"\nCategorías válidas: {', '.join(EXPENSE_CATEGORIES)}."
        ),
    }

    # Build messages: system + history + current
    messages = [system_msg] + _get_history(chat_id) + [{"role": "user", "content": text}]

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
    elif _is_task_add_intent(text):
        tool_choice = {"type": "function", "function": {"name": "add_task"}}
    elif _is_fixed_query_intent(text):
        tool_choice = {"type": "function", "function": {"name": "get_fixed_expenses"}}
    elif _is_fixed_cancel_intent(text):
        tool_choice = {"type": "function", "function": {"name": "cancel_fixed_expense"}}
    elif _is_fixed_add_intent(text):
        tool_choice = {"type": "function", "function": {"name": "add_fixed_expense"}}
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
    elif _is_watch_email_intent(text):
        tool_choice = {"type": "function", "function": {"name": "watch_email"}}
    elif _is_unwatch_email_intent(text):
        tool_choice = {"type": "function", "function": {"name": "unwatch_email"}}
    elif _is_expense_query_intent(text):
        tool_choice = {"type": "function", "function": {"name": "get_expenses"}}
    elif _is_expense_add_intent(text):
        tool_choice = {"type": "function", "function": {"name": "add_expense"}}
    else:
        tool_choice = "auto"
    logger.info(f"tool_choice={tool_choice!r} for: {text[:60]!r}")

    response = await openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        tools=OPENAI_TOOLS,
        tool_choice=tool_choice,
    )

    msg = response.choices[0].message

    if not msg.tool_calls:
        reply = msg.content or "No pude procesar tu mensaje."
        _add_to_history(chat_id, text, reply)
        return reply, None

    messages.append(msg)
    pending_keyboard: InlineKeyboardMarkup | None = None

    for tc in msg.tool_calls:
        func_name = tc.function.name
        func_args = json.loads(tc.function.arguments)
        logger.info(f"Tool call: {func_name}({func_args}) for user {chat_id}")

        if func_name == "delete_event":
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
                _pending_event_creates[chat_id] = {"nombre": nombre, "fecha": fecha, "hora": hora}
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

    final = await openai_client.chat.completions.create(
        model="gpt-4o-mini", messages=messages
    )
    reply = final.choices[0].message.content or "Listo."
    _add_to_history(chat_id, text, reply)
    return reply, pending_keyboard


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
                else f"⚠️ No encontré la tarea n.° {pos}.\n\n"
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
                    "Complete primero la configuración de Google."
                )
        else:
            footer = await build_tasks_footer(user)
            await message.reply_text(
                "⚠️ Use .texto para agregar o .número para eliminar.\n\n" + footer,
                parse_mode="Markdown",
            )
        return

    # OpenAI function calling
    try:
        reply, keyboard = await _call_openai(user, text)
    except GmailPermissionError:
        reply = _gmail_reauth_text(user["chat_id"])
        keyboard = None
    except Exception as e:
        logger.error(f"OpenAI error for user {user['chat_id']}: {e}")
        reply, keyboard = "⚠️ Tuve un error procesando su mensaje. Intente de nuevo.", None

    footer = await build_tasks_footer(user)
    try:
        await message.reply_text(
            reply + "\n\n" + footer, parse_mode="Markdown", reply_markup=keyboard
        )
    except Exception:
        # Reply may contain special chars — send plain, then footer with Markdown
        await message.reply_text(reply, reply_markup=keyboard)
        await message.reply_text(footer, parse_mode="Markdown")


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
        await create_event(user, pending["nombre"], pending["fecha"], pending["hora"])
        await query.edit_message_text(
            f"✅ Evento creado: *{pending['nombre']}*", parse_mode="Markdown"
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


# ── Gmail re-auth helper ──────────────────────────────────────────────────────

def _gmail_reauth_text(chat_id: int) -> str:
    return (
        "⚠️ Su cuenta todavía no tiene permisos de Gmail. "
        "Necesita reautorizar para activar esta función:\n\n"
        f"{BASE_URL}/oauth/start?chat_id={chat_id}"
    )


# ── /vigilar command ──────────────────────────────────────────────────────────

async def vigilar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = await get_user(chat_id)
    if not user or not user.get("access_token"):
        await update.message.reply_text("⚠️ Primero necesita conectar su cuenta de Google.")
        return

    if not context.args:
        watches = await get_email_watches(chat_id)
        if not watches:
            await update.message.reply_text(
                "No está vigilando ningún correo.\n\nUse: /vigilar correo@ejemplo.com"
            )
        else:
            lines = ["📧 *Correos vigilados:*"]
            for w in watches:
                lines.append(f"• {w['email_address']}")
            lines.append("\nUse /vigilar\\_stop correo@ejemplo.com para dejar de vigilar.")
            await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    email_address = context.args[0].lower().strip()
    ok = await add_email_watch(chat_id, email_address)
    if ok:
        await update.message.reply_text(
            f"✅ Le avisaré cuando llegue un correo de *{email_address}*.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text("⚠️ No se pudo guardar. Intente de nuevo.")


async def vigilar_stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Use: /vigilar_stop correo@ejemplo.com")
        return
    email_address = context.args[0].lower().strip()
    await remove_email_watch(chat_id, email_address)
    await update.message.reply_text(
        f"✅ Dejé de vigilar *{email_address}*.", parse_mode="Markdown"
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
            f"⚠️ Todavía no conectó su cuenta de Google.\n\n"
            f"Complete la configuración aquí:\n{oauth_url}"
        )
        return

    if user.get("estado_suscripcion") not in ("activo", "trial"):
        await message.reply_text(
            f"⚠️ Su suscripción no está activa.\n\nActive su plan aquí:\n{PAYMENT_LINK}"
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

    if user and user.get("access_token"):
        await update.message.reply_text(
            "👋 ¡Hola! Ya está configurado y listo.\n\n"
            "Puede decirme:\n"
            "• .texto → agregar tarea\n"
            "• .número → eliminar tarea por número\n"
            "• 'qué tengo hoy' → ver eventos del día\n"
            "• 'crear reunión el viernes a las 10' → agregar evento\n"
            "• Audio de voz 🎤 → lo transcribo y proceso\n\n"
            "Para reconectar su cuenta de Google use /reconectar"
        )
    else:
        await _start_onboarding(update)


async def reconectar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    nombre = update.effective_user.first_name or "Usuario"
    if not await get_user(chat_id):
        await create_user(chat_id, nombre)
    oauth_url = f"{BASE_URL}/oauth/start?chat_id={chat_id}"
    await update.message.reply_text(
        f"🔗 Haga clic aquí para reconectar su cuenta de Google:\n{oauth_url}\n\n"
        "Sus tareas y datos no se borrarán."
    )


async def _start_onboarding(update: Update) -> None:
    chat_id = update.effective_chat.id
    nombre = update.effective_user.first_name or "Usuario"

    if not await get_user(chat_id):
        await create_user(chat_id, nombre)

    oauth_url = f"{BASE_URL}/oauth/start?chat_id={chat_id}"
    await update.message.reply_text(
        f"👋 ¡Hola {nombre}! Bienvenido a su asistente personal.\n\n"
        f"Para empezar, necesito conectar su cuenta de Google. "
        f"Esto me da acceso a su Calendar y crea su hoja de tareas en Google Sheets.\n\n"
        f"👉 Haga clic aquí para autorizar:\n{oauth_url}\n\n"
        f"Una vez que complete la autorización, ¡ya puede usar el bot!"
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
    app.add_handler(CommandHandler("vigilar", vigilar_command))
    app.add_handler(CommandHandler("vigilar_stop", vigilar_stop_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(MessageHandler(filters.TEXT | filters.VOICE, handle_message))
    app.add_handler(CallbackQueryHandler(handle_cal_nav, pattern=r"^calNav_|^calIgnore$"))
    app.add_handler(CallbackQueryHandler(handle_cal_day, pattern=r"^calDay_"))
    app.add_handler(CallbackQueryHandler(handle_delete_event, pattern=r"^delEvent"))
    app.add_handler(CallbackQueryHandler(handle_create_conflict, pattern=r"^createConflict_"))

    logger.info("Bot starting — polling for updates")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
