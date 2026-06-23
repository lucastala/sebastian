"""Migración única: copia la data de cada Google Sheet → Supabase.

Lee, por cada usuario con sheets_id, todas las pestañas de su planilla y las
inserta en las tablas nuevas de Supabase. Es RE-EJECUTABLE: por cada usuario
borra primero su data en las tablas nuevas y la vuelve a insertar, así no
duplica si lo corrés más de una vez.

Uso (con el .env de producción cargado):
    python migrate_to_supabase.py
"""

import asyncio
import logging

import gspread

from database import get_supabase
from data_store import _parse_monto
from google_services import GoogleAuthExpiredError, refresh_user_credentials

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def _active(value) -> bool:
    return str(value).strip().lower() in ("si", "sí", "true", "1")


def _rows(ws_records, chat_id, transform):
    out = []
    for r in ws_records:
        row = transform(r)
        if row:
            row["chat_id"] = chat_id
            out.append(row)
    return out


# pestaña → (tabla destino, función que transforma cada fila)
TABS = {
    "Tareas": ("tareas", lambda r: {
        "tarea": str(r.get("tarea", "")).strip(),
        "estado": str(r.get("estado", "pendiente")).strip() or "pendiente",
        "fecha": str(r.get("fecha", "") or "").strip(),
    } if str(r.get("tarea", "")).strip() else None),
    "Gastos": ("gastos", lambda r: {
        "fecha": str(r.get("fecha", "") or "").strip() or None,
        "monto": _parse_monto(r.get("monto")),
        "categoria": str(r.get("categoria", "") or "").strip(),
        "descripcion": str(r.get("descripcion", "") or "").strip(),
    } if r.get("monto") not in (None, "", 0) else None),
    "Ingresos": ("ingresos", lambda r: {
        "fecha": str(r.get("fecha", "") or "").strip() or None,
        "monto": _parse_monto(r.get("monto")),
        "descripcion": str(r.get("descripcion", "") or "").strip(),
    } if r.get("monto") not in (None, "", 0) else None),
    "GastosFijos": ("gastos_fijos", lambda r: {
        "nombre": str(r.get("nombre", "")).strip(),
        "monto": _parse_monto(r.get("monto")),
        "categoria": str(r.get("categoria", "") or "").strip(),
        "dia_del_mes": int(r.get("dia_del_mes") or 1),
        "activo": _active(r.get("activo")),
        "ultimo_mes_cargado": str(r.get("ultimo_mes_cargado", "") or "").strip() or None,
    } if str(r.get("nombre", "")).strip() else None),
    "Deudas": ("deudas", lambda r: {
        "persona": str(r.get("persona", "") or "").strip(),
        "monto": _parse_monto(r.get("monto")),
        "tipo": "me_deben" if str(r.get("tipo", "")).strip() == "me_deben" else "debo",
        "fecha": str(r.get("fecha", "") or "").strip() or None,
    } if r.get("monto") not in (None, "", 0) else None),
    "Supermercado": ("supermercado", lambda r: {
        "item": str(r.get("item", "")).strip(),
    } if str(r.get("item", "")).strip() else None),
    "Listados": ("listados", lambda r: {
        "lista": str(r.get("lista", "")).strip(),
        "item": str(r.get("item", "")).strip(),
    } if str(r.get("lista", "")).strip() and str(r.get("item", "")).strip() else None),
}


async def migrate_user(user: dict) -> None:
    chat_id = user["chat_id"]
    nombre = user.get("nombre", "?")
    sheets_id = user.get("sheets_id")
    if not sheets_id:
        logger.info(f"  · {nombre} ({chat_id}): sin sheets_id, salteo")
        return

    try:
        creds = await refresh_user_credentials(user)
    except GoogleAuthExpiredError:
        logger.info(f"  ⚠️  {nombre} ({chat_id}): token vencido, NO migrado (tendrá que reconectar)")
        return
    except Exception as e:
        logger.info(f"  ⚠️  {nombre} ({chat_id}): error de credenciales ({e}), salteo")
        return

    sb = get_supabase()
    gc = gspread.authorize(creds)
    try:
        sh = gc.open_by_key(sheets_id)
    except Exception as e:
        logger.info(f"  ⚠️  {nombre} ({chat_id}): no se pudo abrir el Sheet ({e}), salteo")
        return

    resumen = []
    for tab, (tabla, transform) in TABS.items():
        try:
            ws = sh.worksheet(tab)
        except gspread.WorksheetNotFound:
            continue
        rows = _rows(ws.get_all_records(), chat_id, transform)
        # limpiar lo previo de este usuario en esa tabla (re-ejecutable)
        sb.table(tabla).delete().eq("chat_id", chat_id).execute()
        if rows:
            sb.table(tabla).insert(rows).execute()
        resumen.append(f"{tabla}:{len(rows)}")

    logger.info(f"  ✅ {nombre} ({chat_id}): " + ", ".join(resumen))


async def main() -> None:
    res = get_supabase().table("usuarios").select("*").not_.is_("sheets_id", "null").execute()
    users = res.data or []
    logger.info(f"Migrando {len(users)} usuarios con planilla...\n")
    for user in users:
        await migrate_user(user)
    logger.info("\nListo. Verificá las tablas en Supabase antes de hacer el switch (Fase 4).")


if __name__ == "__main__":
    asyncio.run(main())
