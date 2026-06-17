import os
import logging
from datetime import datetime, timezone

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


async def update_user_genero(chat_id: int, genero: str) -> bool:
    """Set the user's address preference: 'm' (señor) or 'f' (señora). Returns success."""
    try:
        get_supabase().table("usuarios").update({"genero": genero}).eq(
            "chat_id", chat_id
        ).execute()
        return True
    except Exception as e:
        logger.error(f"Error updating genero for {chat_id}: {e}")
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
