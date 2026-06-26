import os
import logging
import secrets
import string
from datetime import datetime, timedelta, timezone

from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_supabase: Client | None = None


def get_supabase() -> Client:
    global _supabase
    if _supabase is None:
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_KEY")
        if not url or not key:
            raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set in .env")
        _supabase = create_client(url, key)
    return _supabase


async def get_user(chat_id: int) -> dict | None:
    try:
        result = get_supabase().table("usuarios").select("*").eq("chat_id", chat_id).execute()
        return result.data[0] if result.data else None
    except Exception as e:
        logger.error(f"Error getting user {chat_id}: {e}")
        return None


async def create_user(chat_id: int, nombre: str) -> dict:
    data = {
        "chat_id": chat_id,
        "nombre": nombre,
        "estado_suscripcion": "trial",
        "fecha_alta": datetime.now(timezone.utc).isoformat(),
    }
    result = get_supabase().table("usuarios").insert(data).execute()
    return result.data[0] if result.data else data


async def update_user_tokens(
    chat_id: int,
    access_token: str,
    refresh_token: str | None,
    token_expiry: datetime | None,
    email: str,
) -> None:
    expiry_str = None
    if token_expiry:
        if token_expiry.tzinfo is None:
            token_expiry = token_expiry.replace(tzinfo=timezone.utc)
        expiry_str = token_expiry.isoformat()

    get_supabase().table("usuarios").update(
        {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_expiry": expiry_str,
            "email": email,
        }
    ).eq("chat_id", chat_id).execute()


async def update_user_sheet_id(chat_id: int, sheets_id: str) -> None:
    get_supabase().table("usuarios").update({"sheets_id": sheets_id}).eq(
        "chat_id", chat_id
    ).execute()


async def update_user_resumen(chat_id: int, hora: int | None) -> bool:
    """Hora (0-23) del resumen diario, o None para apagarlo. Devuelve éxito."""
    try:
        get_supabase().table("usuarios").update({"hora_resumen": hora}).eq(
            "chat_id", chat_id
        ).execute()
        return True
    except Exception as e:
        logger.error(f"Error updating hora_resumen for {chat_id}: {e}")
        return False


async def wipe_user_account_extras(chat_id: int) -> None:
    """Para 'borrar mis datos': elimina recordatorios y desconecta Google (limpia tokens).
    NO borra la fila de usuarios ni toca la suscripción."""
    sb = get_supabase()
    try:
        sb.table("recordatorios").delete().eq("chat_id", chat_id).execute()
    except Exception as e:
        logger.error(f"Error deleting recordatorios for {chat_id}: {e}")
    try:
        sb.table("usuarios").update({
            "access_token": None, "refresh_token": None,
            "token_expiry": None, "email": None,
        }).eq("chat_id", chat_id).execute()
    except Exception as e:
        logger.error(f"Error clearing Google tokens for {chat_id}: {e}")


async def update_user_tratamiento(chat_id: int, tratamiento: str | None) -> bool:
    """Cómo quiere la persona que la llamen (nombre/título), o None para trato neutro."""
    try:
        get_supabase().table("usuarios").update({"tratamiento": tratamiento}).eq(
            "chat_id", chat_id
        ).execute()
        return True
    except Exception as e:
        logger.error(f"Error updating tratamiento for {chat_id}: {e}")
        return False


async def check_subscription(chat_id: int) -> bool:
    user = await get_user(chat_id)
    if not user:
        return False
    return user.get("estado_suscripcion") in ("activo", "trial")


async def get_active_users() -> list[dict]:
    try:
        result = (
            get_supabase()
            .table("usuarios")
            .select("*")
            .in_("estado_suscripcion", ["activo", "trial"])
            .execute()
        )
        return result.data or []
    except Exception as e:
        logger.error(f"Error fetching active users: {e}")
        return []


# ── OAuth flows (binding seguro del chat_id) ──────────────────────────────────
# El bot emite un token opaco atado al chat_id real (autenticado por Telegram);
# el servidor resuelve el chat_id desde la base, nunca desde la URL. Uso único.

async def create_oauth_flow(chat_id: int) -> str:
    token = secrets.token_urlsafe(32)
    get_supabase().table("oauth_flows").insert(
        {
            "token": token,
            "chat_id": chat_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    ).execute()
    return token


async def get_oauth_flow(token: str) -> dict | None:
    try:
        result = (
            get_supabase().table("oauth_flows").select("*").eq("token", token).execute()
        )
        return result.data[0] if result.data else None
    except Exception as e:
        logger.error(f"Error getting oauth flow: {e}")
        return None


async def set_oauth_flow_verifier(token: str, code_verifier: str) -> None:
    get_supabase().table("oauth_flows").update(
        {"code_verifier": code_verifier}
    ).eq("token", token).execute()


async def delete_oauth_flow(token: str) -> None:
    get_supabase().table("oauth_flows").delete().eq("token", token).execute()


# ── Recordatorios ─────────────────────────────────────────────────────────────

async def add_reminder(chat_id: int, texto: str, fecha_hora_iso: str) -> bool:
    try:
        get_supabase().table("recordatorios").insert({
            "chat_id": chat_id,
            "texto": texto,
            "fecha_hora": fecha_hora_iso,
            "enviado": False,
        }).execute()
        return True
    except Exception as e:
        logger.error(f"Error adding reminder for {chat_id}: {e}")
        return False


async def get_due_reminders() -> list[dict]:
    try:
        now = datetime.now(timezone.utc).isoformat()
        result = (
            get_supabase()
            .table("recordatorios")
            .select("*")
            .lte("fecha_hora", now)
            .eq("enviado", False)
            .execute()
        )
        return result.data or []
    except Exception as e:
        logger.error(f"Error fetching due reminders: {e}")
        return []


async def mark_reminder_sent(reminder_id: int) -> None:
    try:
        get_supabase().table("recordatorios").update({"enviado": True}).eq(
            "id", reminder_id
        ).execute()
    except Exception as e:
        logger.error(f"Error marking reminder {reminder_id} sent: {e}")


async def get_user_reminders(chat_id: int) -> list[dict]:
    try:
        result = (
            get_supabase()
            .table("recordatorios")
            .select("*")
            .eq("chat_id", chat_id)
            .eq("enviado", False)
            .order("fecha_hora")
            .execute()
        )
        return result.data or []
    except Exception as e:
        logger.error(f"Error fetching reminders for {chat_id}: {e}")
        return []


async def delete_reminder(reminder_id: int) -> None:
    try:
        get_supabase().table("recordatorios").delete().eq("id", reminder_id).execute()
    except Exception as e:
        logger.error(f"Error deleting reminder {reminder_id}: {e}")


# ── Suscripción / códigos de activación ───────────────────────────────────────

SUB_DAYS = 30


def _generate_code() -> str:
    """Código formato SEB-XXXXX (5 alfanuméricos en mayúscula)."""
    alphabet = string.ascii_uppercase + string.digits
    return "SEB-" + "".join(secrets.choice(alphabet) for _ in range(5))


async def _code_exists(codigo: str) -> bool:
    try:
        result = get_supabase().table("codigos_activacion").select("id").eq(
            "codigo", codigo
        ).execute()
        return bool(result.data)
    except Exception as e:
        logger.error(f"Error checking code {codigo}: {e}")
        return False


async def create_activation_code(email: str, mp_payment_id: str | None = None) -> str | None:
    """Genera un código único y lo guarda como sin_usar. Devuelve el código."""
    try:
        codigo = _generate_code()
        for _ in range(10):  # asegurar unicidad
            if not await _code_exists(codigo):
                break
            codigo = _generate_code()
        get_supabase().table("codigos_activacion").insert({
            "codigo": codigo,
            "email_comprador": email,
            "estado": "sin_usar",
            "mp_payment_id": mp_payment_id,
        }).execute()
        return codigo
    except Exception as e:
        logger.error(f"Error creating activation code for {email}: {e}")
        return None


async def get_activation_code(codigo: str) -> dict | None:
    try:
        result = get_supabase().table("codigos_activacion").select("*").eq(
            "codigo", codigo.upper().strip()
        ).execute()
        return result.data[0] if result.data else None
    except Exception as e:
        logger.error(f"Error fetching code {codigo}: {e}")
        return None


async def use_activation_code(codigo: str, chat_id: int) -> bool:
    """Marca un código como usado (si estaba sin_usar). Devuelve True si se pudo."""
    try:
        row = await get_activation_code(codigo)
        if not row or row.get("estado") != "sin_usar":
            return False
        get_supabase().table("codigos_activacion").update({
            "estado": "usado",
            "chat_id": chat_id,
            "fecha_uso": datetime.now(timezone.utc).isoformat(),
        }).eq("codigo", row["codigo"]).execute()
        return True
    except Exception as e:
        logger.error(f"Error using code {codigo}: {e}")
        return False


async def activate_user(chat_id: int, nombre: str, dias: int = SUB_DAYS) -> None:
    """Marca al usuario como activo con vencimiento en `dias` días (lo crea si no existe)."""
    venc = (datetime.now(timezone.utc) + timedelta(days=dias)).isoformat()
    existing = await get_user(chat_id)
    if existing:
        get_supabase().table("usuarios").update({
            "estado_suscripcion": "activo",
            "fecha_vencimiento": venc,
        }).eq("chat_id", chat_id).execute()
    else:
        get_supabase().table("usuarios").insert({
            "chat_id": chat_id,
            "nombre": nombre,
            "estado_suscripcion": "activo",
            "fecha_vencimiento": venc,
            "fecha_alta": datetime.now(timezone.utc).isoformat(),
        }).execute()


async def get_user_by_email(email: str) -> dict | None:
    try:
        result = get_supabase().table("usuarios").select("*").eq(
            "email", email
        ).execute()
        return result.data[0] if result.data else None
    except Exception as e:
        logger.error(f"Error fetching user by email {email}: {e}")
        return None


async def extend_subscription_by_email(email: str, dias: int = SUB_DAYS) -> int | None:
    """Para pagos recurrentes: extiende el vencimiento del usuario con ese email.
    Devuelve el chat_id si se encontró, o None."""
    # Buscar primero por código usado (mapea email → chat_id), luego por usuario.
    chat_id = None
    try:
        result = get_supabase().table("codigos_activacion").select("chat_id").eq(
            "email_comprador", email
        ).not_.is_("chat_id", "null").order("fecha_uso", desc=True).execute()
        if result.data:
            chat_id = result.data[0]["chat_id"]
    except Exception as e:
        logger.error(f"Error mapping email→chat_id for {email}: {e}")

    if chat_id is None:
        user = await get_user_by_email(email)
        chat_id = user["chat_id"] if user else None

    if chat_id is None:
        return None

    venc = (datetime.now(timezone.utc) + timedelta(days=dias)).isoformat()
    get_supabase().table("usuarios").update({
        "estado_suscripcion": "activo",
        "fecha_vencimiento": venc,
    }).eq("chat_id", chat_id).execute()
    return chat_id


async def get_expired_active_users() -> list[dict]:
    """Usuarios activos cuyo vencimiento ya pasó."""
    try:
        now = datetime.now(timezone.utc).isoformat()
        result = (
            get_supabase()
            .table("usuarios")
            .select("*")
            .eq("estado_suscripcion", "activo")
            .not_.is_("fecha_vencimiento", "null")
            .lt("fecha_vencimiento", now)
            .execute()
        )
        return result.data or []
    except Exception as e:
        logger.error(f"Error fetching expired users: {e}")
        return []


async def set_user_inactive(chat_id: int) -> None:
    try:
        get_supabase().table("usuarios").update({
            "estado_suscripcion": "inactivo",
        }).eq("chat_id", chat_id).execute()
    except Exception as e:
        logger.error(f"Error setting user {chat_id} inactive: {e}")
