"""Acceso a datos en Supabase (tareas, gastos, listas, etc.).

Reemplaza a la versión basada en Google Sheets de google_services.py. Mantiene
las MISMAS firmas que las funciones viejas, así el resto del bot no cambia: cada
función recibe `user: dict` y usa user["chat_id"]. La data ya no vive en una
planilla del usuario, vive en Supabase (sin cuota por minuto, mucho más rápido).
"""

import logging
from datetime import datetime, timedelta, timezone

from database import get_supabase

logger = logging.getLogger(__name__)

ARGENTINA_TZ = timezone(timedelta(hours=-3))


def _parse_monto(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        s = str(value).replace("$", "").replace(" ", "").strip()
        if "," in s and "." in s:
            s = s.replace(".", "").replace(",", ".")
        elif "," in s:
            s = s.replace(",", ".")
        return float(s)
    except (ValueError, TypeError):
        return 0.0


def _today() -> str:
    return datetime.now(ARGENTINA_TZ).strftime("%Y-%m-%d")


# ── Tareas ────────────────────────────────────────────────────────────────────

def _sort_pending(rows: list[dict]) -> list[dict]:
    """Sin fecha primero, luego con fecha de más lejana a más cercana (igual que el footer)."""
    no_date = [r for r in rows if not str(r.get("fecha") or "").strip()]
    dated = sorted(
        [r for r in rows if str(r.get("fecha") or "").strip()],
        key=lambda r: str(r["fecha"]), reverse=True,
    )
    return no_date + dated


async def get_pending_tasks(user: dict) -> list[dict]:
    res = get_supabase().table("tareas").select("*").eq(
        "chat_id", user["chat_id"]
    ).eq("estado", "pendiente").execute()
    return res.data or []


async def add_task(user: dict, tarea: str) -> str | None:
    try:
        res = get_supabase().table("tareas").insert({
            "chat_id": user["chat_id"], "tarea": tarea, "estado": "pendiente", "fecha": "",
        }).execute()
        return str(res.data[0]["id"]) if res.data else None
    except Exception as e:
        logger.error(f"Error adding task for {user.get('chat_id')}: {e}")
        return None


async def update_task_fecha(user: dict, task_id: str, fecha: str) -> bool:
    try:
        get_supabase().table("tareas").update({"fecha": fecha}).eq(
            "id", int(task_id)
        ).eq("chat_id", user["chat_id"]).execute()
        return True
    except Exception as e:
        logger.error(f"Error updating task fecha: {e}")
        return False


async def delete_task_by_position(user: dict, position: int) -> str | None:
    rows = _sort_pending(await get_pending_tasks(user))
    if position < 1 or position > len(rows):
        return None
    row = rows[position - 1]
    get_supabase().table("tareas").update({"estado": "completada"}).eq(
        "id", row["id"]
    ).execute()
    return row.get("tarea", "")


async def update_task(user: dict, posicion: int, nuevo_nombre: str | None = None,
                      nueva_fecha: str | None = None) -> bool:
    rows = _sort_pending(await get_pending_tasks(user))
    if posicion < 1 or posicion > len(rows):
        return False
    row = rows[posicion - 1]
    update = {}
    if nuevo_nombre:
        update["tarea"] = nuevo_nombre
    if nueva_fecha is not None:
        update["fecha"] = nueva_fecha
    if update:
        get_supabase().table("tareas").update(update).eq("id", row["id"]).execute()
    return True


# ── Gastos ────────────────────────────────────────────────────────────────────

def _filter_gastos(rows: list[dict], desde, hasta, categoria) -> list[dict]:
    out = []
    for r in rows:
        f = str(r.get("fecha") or "").strip()
        if desde and f < desde:
            continue
        if hasta and f > hasta:
            continue
        if categoria and str(r.get("categoria") or "").strip().lower() != categoria.lower():
            continue
        out.append(r)
    out.sort(key=lambda r: r["id"], reverse=True)  # más reciente primero
    return out


async def _all_gastos(chat_id: int) -> list[dict]:
    res = get_supabase().table("gastos").select("*").eq("chat_id", chat_id).execute()
    return res.data or []


async def add_expense(user: dict, monto, categoria: str, descripcion: str = "",
                      fecha: str | None = None, medio_pago=None) -> bool:
    try:
        get_supabase().table("gastos").insert({
            "chat_id": user["chat_id"], "fecha": fecha or _today(),
            "monto": _parse_monto(monto), "categoria": categoria, "descripcion": descripcion,
        }).execute()
        return True
    except Exception as e:
        logger.error(f"Error adding expense: {e}")
        return False


async def get_expenses(user: dict, desde=None, hasta=None, categoria=None) -> dict:
    rows = _filter_gastos(await _all_gastos(user["chat_id"]), desde, hasta, categoria)
    por_cat: dict[str, float] = {}
    total = 0.0
    gastos = []
    for pos, r in enumerate(rows, 1):
        m = _parse_monto(r.get("monto"))
        total += m
        cat = str(r.get("categoria") or "Otros").strip() or "Otros"
        por_cat[cat] = por_cat.get(cat, 0.0) + m
        gastos.append({"n": pos, "fecha": r.get("fecha", ""), "monto": m,
                       "categoria": cat, "descripcion": r.get("descripcion", "")})
    return {"total": total, "count": len(rows), "por_categoria": por_cat, "gastos": gastos}


async def update_expense_monto(user: dict, posicion: int, nuevo_monto,
                               desde=None, hasta=None, categoria=None) -> dict | None:
    rows = _filter_gastos(await _all_gastos(user["chat_id"]), desde, hasta, categoria)
    if posicion < 1 or posicion > len(rows):
        return None
    r = rows[posicion - 1]
    get_supabase().table("gastos").update({"monto": _parse_monto(nuevo_monto)}).eq(
        "id", r["id"]
    ).execute()
    return {"descripcion": r.get("descripcion", ""), "monto": _parse_monto(nuevo_monto),
            "categoria": r.get("categoria", "")}


async def delete_expense(user: dict, posicion: int, desde=None, hasta=None,
                         categoria=None) -> dict | None:
    rows = _filter_gastos(await _all_gastos(user["chat_id"]), desde, hasta, categoria)
    if posicion < 1 or posicion > len(rows):
        return None
    r = rows[posicion - 1]
    get_supabase().table("gastos").delete().eq("id", r["id"]).execute()
    return {"descripcion": r.get("descripcion", ""), "monto": _parse_monto(r.get("monto")),
            "categoria": r.get("categoria", "")}


# ── Ingresos y balance ────────────────────────────────────────────────────────

async def add_income(user: dict, monto, descripcion: str = "", fecha: str | None = None) -> bool:
    try:
        get_supabase().table("ingresos").insert({
            "chat_id": user["chat_id"], "fecha": fecha or _today(),
            "monto": _parse_monto(monto), "descripcion": descripcion,
        }).execute()
        return True
    except Exception as e:
        logger.error(f"Error adding income: {e}")
        return False


async def get_balance(user: dict, desde=None, hasta=None) -> dict:
    def _in_range(f):
        f = str(f or "").strip()
        if desde and f < desde:
            return False
        if hasta and f > hasta:
            return False
        return True

    gastos_total = sum(
        _parse_monto(r.get("monto")) for r in await _all_gastos(user["chat_id"])
        if _in_range(r.get("fecha"))
    )
    ing = get_supabase().table("ingresos").select("*").eq("chat_id", user["chat_id"]).execute()
    ingresos_total = sum(
        _parse_monto(r.get("monto")) for r in (ing.data or []) if _in_range(r.get("fecha"))
    )
    return {"ingresos": ingresos_total, "gastos": gastos_total,
            "balance": ingresos_total - gastos_total}


# ── Gastos fijos ──────────────────────────────────────────────────────────────

def _is_active(value) -> bool:
    return value is True or str(value).strip().lower() in ("true", "si", "sí", "1")


async def _all_fijos(chat_id: int) -> list[dict]:
    res = get_supabase().table("gastos_fijos").select("*").eq("chat_id", chat_id).execute()
    return res.data or []


async def add_fixed_expense(user: dict, nombre: str, monto, categoria: str,
                            dia_del_mes: int = 1) -> bool:
    try:
        existing = [
            r for r in await _all_fijos(user["chat_id"])
            if str(r.get("nombre", "")).strip().lower() == nombre.strip().lower()
        ]
        data = {"monto": _parse_monto(monto), "categoria": categoria,
                "dia_del_mes": int(dia_del_mes), "activo": True}
        if existing:
            get_supabase().table("gastos_fijos").update(data).eq("id", existing[0]["id"]).execute()
        else:
            data.update({"chat_id": user["chat_id"], "nombre": nombre.strip(),
                         "ultimo_mes_cargado": None})
            get_supabase().table("gastos_fijos").insert(data).execute()
        return True
    except Exception as e:
        logger.error(f"Error adding fixed expense: {e}")
        return False


async def get_fixed_expenses(user: dict, solo_activos: bool = True) -> list[dict]:
    rows = await _all_fijos(user["chat_id"])
    if solo_activos:
        rows = [r for r in rows if _is_active(r.get("activo"))]
    return rows


async def cancel_fixed_expense(user: dict, nombre: str) -> str | None:
    q = nombre.strip().lower()
    for r in await _all_fijos(user["chat_id"]):
        if _is_active(r.get("activo")) and q in str(r.get("nombre", "")).strip().lower():
            get_supabase().table("gastos_fijos").update({"activo": False}).eq("id", r["id"]).execute()
            return r.get("nombre", nombre)
    return None


async def log_due_fixed_expenses(user: dict, today: datetime) -> list[dict]:
    current_month = today.strftime("%Y-%m")
    today_str = today.strftime("%Y-%m-%d")
    logged = []
    for r in await _all_fijos(user["chat_id"]):
        if not _is_active(r.get("activo")):
            continue
        if str(r.get("ultimo_mes_cargado") or "").strip() == current_month:
            continue
        try:
            dia = int(r.get("dia_del_mes") or 1)
        except (ValueError, TypeError):
            dia = 1
        if today.day < dia:
            continue
        nombre = r.get("nombre", "")
        monto = _parse_monto(r.get("monto"))
        categoria = r.get("categoria", "Otros")
        get_supabase().table("gastos").insert({
            "chat_id": user["chat_id"], "fecha": today_str, "monto": monto,
            "categoria": categoria, "descripcion": f"{nombre} (fijo)",
        }).execute()
        get_supabase().table("gastos_fijos").update({
            "ultimo_mes_cargado": current_month
        }).eq("id", r["id"]).execute()
        logged.append({"nombre": nombre, "monto": monto, "categoria": categoria})
    return logged


# ── Deudas ────────────────────────────────────────────────────────────────────

async def add_debt(user: dict, persona: str, monto, tipo: str = "debo",
                   fecha: str | None = None) -> bool:
    try:
        get_supabase().table("deudas").insert({
            "chat_id": user["chat_id"], "persona": persona, "monto": _parse_monto(monto),
            "tipo": "me_deben" if tipo == "me_deben" else "debo", "fecha": fecha or _today(),
        }).execute()
        return True
    except Exception as e:
        logger.error(f"Error adding debt: {e}")
        return False


async def get_debts(user: dict) -> dict:
    res = get_supabase().table("deudas").select("*").eq(
        "chat_id", user["chat_id"]
    ).order("id").execute()
    deudas, total_debo, total_me = [], 0.0, 0.0
    for i, r in enumerate(res.data or [], 1):
        m = _parse_monto(r.get("monto"))
        tipo = str(r.get("tipo", "debo")).strip()
        deudas.append({"n": i, "persona": r.get("persona", ""), "monto": m, "tipo": tipo})
        if tipo == "me_deben":
            total_me += m
        else:
            total_debo += m
    return {"deudas": deudas, "total_debo": total_debo, "total_me_deben": total_me}


async def settle_debt(user: dict, posiciones: list[int]) -> list[str]:
    res = get_supabase().table("deudas").select("*").eq(
        "chat_id", user["chat_id"]
    ).order("id").execute()
    rows = res.data or []
    saldadas = []
    for p in sorted({int(x) for x in posiciones}, reverse=True):
        if 1 <= p <= len(rows):
            r = rows[p - 1]
            get_supabase().table("deudas").delete().eq("id", r["id"]).execute()
            saldadas.append(str(r.get("persona", "")))
    return saldadas


# ── Lista de supermercado ─────────────────────────────────────────────────────

async def add_super_item(user: dict, item: str) -> bool:
    try:
        get_supabase().table("supermercado").insert({
            "chat_id": user["chat_id"], "item": item,
        }).execute()
        return True
    except Exception as e:
        logger.error(f"Error adding super item: {e}")
        return False


async def get_super_list(user: dict) -> list[dict]:
    res = get_supabase().table("supermercado").select("*").eq(
        "chat_id", user["chat_id"]
    ).order("id").execute()
    return [{"n": i, "item": r.get("item", "")} for i, r in enumerate(res.data or [], 1)]


async def remove_super_items(user: dict, posiciones: list[int]) -> list[str]:
    res = get_supabase().table("supermercado").select("*").eq(
        "chat_id", user["chat_id"]
    ).order("id").execute()
    rows = res.data or []
    quitados = []
    for p in sorted({int(x) for x in posiciones}, reverse=True):
        if 1 <= p <= len(rows):
            r = rows[p - 1]
            get_supabase().table("supermercado").delete().eq("id", r["id"]).execute()
            quitados.append(str(r.get("item", "")))
    return quitados


async def clear_super_list(user: dict) -> int:
    res = get_supabase().table("supermercado").select("id").eq(
        "chat_id", user["chat_id"]
    ).execute()
    n = len(res.data or [])
    if n:
        get_supabase().table("supermercado").delete().eq("chat_id", user["chat_id"]).execute()
    return n


# ── Listados con nombre ───────────────────────────────────────────────────────

def _norm(s) -> str:
    return str(s).strip().lower()


async def add_list_items(user: dict, nombre: str, items: list[str]) -> int:
    items = [i for i in items if str(i).strip()]
    if not items:
        return 0
    try:
        get_supabase().table("listados").insert([
            {"chat_id": user["chat_id"], "lista": nombre.strip(), "item": it} for it in items
        ]).execute()
        return len(items)
    except Exception as e:
        logger.error(f"Error adding list items: {e}")
        return 0


async def _all_listados(chat_id: int) -> list[dict]:
    res = get_supabase().table("listados").select("*").eq(
        "chat_id", chat_id
    ).order("id").execute()
    return res.data or []


async def get_list_items(user: dict, nombre: str) -> list[dict]:
    rows = [r for r in await _all_listados(user["chat_id"]) if _norm(r.get("lista")) == _norm(nombre)]
    return [{"n": i, "item": r.get("item", "")} for i, r in enumerate(rows, 1)]


async def get_list_names(user: dict) -> list[dict]:
    seen: dict[str, list] = {}
    for r in await _all_listados(user["chat_id"]):
        name = str(r.get("lista", "")).strip()
        if not name:
            continue
        key = name.lower()
        if key not in seen:
            seen[key] = [name, 0]
        seen[key][1] += 1
    return [{"nombre": v[0], "items": v[1]} for v in seen.values()]


async def remove_list_items(user: dict, nombre: str, posiciones: list[int]) -> list[str]:
    rows = [r for r in await _all_listados(user["chat_id"]) if _norm(r.get("lista")) == _norm(nombre)]
    quitados = []
    for p in sorted({int(x) for x in posiciones}, reverse=True):
        if 1 <= p <= len(rows):
            r = rows[p - 1]
            get_supabase().table("listados").delete().eq("id", r["id"]).execute()
            quitados.append(str(r.get("item", "")))
    return quitados


async def delete_list(user: dict, nombre: str) -> int:
    rows = [r for r in await _all_listados(user["chat_id"]) if _norm(r.get("lista")) == _norm(nombre)]
    for r in rows:
        get_supabase().table("listados").delete().eq("id", r["id"]).execute()
    return len(rows)
