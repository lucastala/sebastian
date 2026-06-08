import base64
import os
import asyncio
import calendar as cal_module
import logging
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from typing import Optional

import gspread
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


class GmailPermissionError(Exception):
    pass

load_dotenv()

logger = logging.getLogger(__name__)

ARGENTINA_TZ = timezone(timedelta(hours=-3))

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]


def _build_credentials(user: dict) -> Credentials:
    return Credentials(
        token=user["access_token"],
        refresh_token=user["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
        scopes=SCOPES,
    )


async def refresh_user_credentials(user: dict) -> Credentials:
    creds = _build_credentials(user)

    if not creds.valid:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: creds.refresh(Request()))

        from database import update_user_tokens

        await update_user_tokens(
            chat_id=user["chat_id"],
            access_token=creds.token,
            refresh_token=creds.refresh_token,
            token_expiry=creds.expiry,
            email=user.get("email", ""),
        )

    return creds


# ── Google Sheets ─────────────────────────────────────────────────────────────


async def create_user_sheet(credentials: Credentials) -> str:
    """Create a new Google Sheet in the user's Drive and return its ID."""
    loop = asyncio.get_running_loop()

    def _create():
        gc = gspread.authorize(credentials)
        sh = gc.create("Sebastian SaaS — Tareas")
        ws = sh.sheet1
        ws.update_title("Tareas")
        ws.append_row(["id", "tarea", "estado", "prioridad", "fecha"])
        return sh.id

    return await loop.run_in_executor(None, _create)


async def get_pending_tasks(user: dict) -> list[dict]:
    if not user.get("sheets_id"):
        return []

    creds = await refresh_user_credentials(user)
    loop = asyncio.get_running_loop()

    def _get():
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(user["sheets_id"])
        records = sh.sheet1.get_all_records()
        return [r for r in records if str(r.get("estado", "")).lower() == "pendiente"]

    return await loop.run_in_executor(None, _get)


async def add_task(user: dict, tarea: str) -> str | None:
    """Add task and return its id, or None if sheets_id is not set."""
    if not user.get("sheets_id"):
        return None

    creds = await refresh_user_credentials(user)
    task_id = str(int(datetime.now().timestamp() * 1000))
    loop = asyncio.get_running_loop()

    def _add():
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(user["sheets_id"])
        sh.sheet1.append_row([task_id, tarea, "pendiente", "", ""])
        return task_id

    return await loop.run_in_executor(None, _add)


async def update_task_fecha(user: dict, task_id: str, fecha: str) -> bool:
    """Set the fecha column for a specific task by its id."""
    if not user.get("sheets_id"):
        return False

    creds = await refresh_user_credentials(user)
    loop = asyncio.get_running_loop()

    def _update():
        gc = gspread.authorize(creds)
        ws = gc.open_by_key(user["sheets_id"]).sheet1
        records = ws.get_all_records()
        all_ids = [str(r.get("id", "")) for r in records]
        if task_id not in all_ids:
            return False
        row_idx = all_ids.index(task_id) + 2  # +2: header row + 0-index
        headers = ws.row_values(1)
        fecha_col = headers.index("fecha") + 1
        ws.update_cell(row_idx, fecha_col, fecha)
        return True

    return await loop.run_in_executor(None, _update)


async def delete_task_by_position(user: dict, position: int) -> str | None:
    """Mark the nth pending task (in display order) as completed. Returns task name or None."""
    if not user.get("sheets_id"):
        return None

    creds = await refresh_user_credentials(user)
    loop = asyncio.get_running_loop()

    def _delete():
        gc = gspread.authorize(creds)
        ws = gc.open_by_key(user["sheets_id"]).sheet1
        records = ws.get_all_records()
        pending = [
            (idx + 2, r)
            for idx, r in enumerate(records)
            if str(r.get("estado", "")).lower() == "pendiente"
        ]
        # Sort to match display order: no-date first, then dated descending
        no_date = [(row, r) for row, r in pending if not str(r.get("fecha", "")).strip()]
        dated = sorted(
            [(row, r) for row, r in pending if str(r.get("fecha", "")).strip()],
            key=lambda x: str(x[1].get("fecha", "")),
            reverse=True,
        )
        sorted_pending = no_date + dated

        if position < 1 or position > len(sorted_pending):
            return None
        row_idx, task = sorted_pending[position - 1]
        ws.update_cell(row_idx, 3, "completada")  # column 3 = estado
        return task.get("tarea", "")

    return await loop.run_in_executor(None, _delete)


# ── Google Calendar ───────────────────────────────────────────────────────────


async def get_today_events(user: dict) -> list[dict]:
    now = datetime.now(ARGENTINA_TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = now.replace(hour=23, minute=59, second=59, microsecond=0)
    return await _get_calendar_events(user, start, end)


async def get_events_by_date(user: dict, fecha: str) -> list[dict]:
    date = datetime.strptime(fecha, "%Y-%m-%d").replace(tzinfo=ARGENTINA_TZ)
    start = date.replace(hour=0, minute=0, second=0, microsecond=0)
    end = date.replace(hour=23, minute=59, second=59, microsecond=0)
    return await _get_calendar_events(user, start, end)


async def _get_calendar_events(
    user: dict, start: datetime, end: datetime
) -> list[dict]:
    creds = await refresh_user_credentials(user)
    loop = asyncio.get_running_loop()

    def _get():
        service = build("calendar", "v3", credentials=creds)
        result = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=start.isoformat(),
                timeMax=end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        events = []
        for ev in result.get("items", []):
            start_val = ev["start"].get("dateTime", ev["start"].get("date", ""))
            events.append(
                {
                    "id": ev.get("id", ""),
                    "nombre": ev.get("summary", "Sin título"),
                    "inicio": start_val,
                    "descripcion": ev.get("description", ""),
                }
            )
        return events

    return await loop.run_in_executor(None, _get)


async def search_event(user: dict, query: str) -> list[dict]:
    creds = await refresh_user_credentials(user)
    loop = asyncio.get_running_loop()

    def _search():
        service = build("calendar", "v3", credentials=creds)
        result = (
            service.events()
            .list(
                calendarId="primary",
                q=query,
                singleEvents=True,
                orderBy="startTime",
                maxResults=10,
            )
            .execute()
        )
        events = []
        for ev in result.get("items", []):
            start_val = ev["start"].get("dateTime", ev["start"].get("date", ""))
            events.append(
                {
                    "id": ev.get("id", ""),
                    "nombre": ev.get("summary", "Sin título"),
                    "inicio": start_val,
                    "descripcion": ev.get("description", ""),
                }
            )
        return events

    return await loop.run_in_executor(None, _search)


async def update_event(
    user: dict,
    event_id: str,
    nuevo_nombre: str | None = None,
    nueva_fecha: str | None = None,
    nueva_hora: str | None = None,
) -> dict:
    creds = await refresh_user_credentials(user)
    loop = asyncio.get_running_loop()

    def _update():
        service = build("calendar", "v3", credentials=creds)
        event = service.events().get(calendarId="primary", eventId=event_id).execute()

        if nuevo_nombre:
            event["summary"] = nuevo_nombre

        if nueva_fecha or nueva_hora:
            if "dateTime" in event.get("start", {}):
                start_dt = datetime.fromisoformat(event["start"]["dateTime"])
                end_dt = datetime.fromisoformat(event["end"]["dateTime"])
                duration = end_dt - start_dt

                if nueva_fecha:
                    d = datetime.strptime(nueva_fecha, "%Y-%m-%d")
                    start_dt = start_dt.replace(year=d.year, month=d.month, day=d.day)
                if nueva_hora:
                    h, m = map(int, nueva_hora.split(":"))
                    start_dt = start_dt.replace(hour=h, minute=m, second=0)

                new_end = start_dt + duration
                tz = "America/Argentina/Buenos_Aires"
                event["start"] = {"dateTime": start_dt.strftime("%Y-%m-%dT%H:%M:00"), "timeZone": tz}
                event["end"] = {"dateTime": new_end.strftime("%Y-%m-%dT%H:%M:00"), "timeZone": tz}
            else:
                if nueva_fecha:
                    event["start"] = {"date": nueva_fecha}
                    event["end"] = {"date": nueva_fecha}

        updated = service.events().update(
            calendarId="primary", eventId=event_id, body=event
        ).execute()
        return {"nombre": updated.get("summary"), "id": updated.get("id")}

    return await loop.run_in_executor(None, _update)


async def update_task(user: dict, posicion: int, nuevo_nombre: str | None = None, nueva_fecha: str | None = None) -> bool:
    """Update task name and/or date by its display position."""
    if not user.get("sheets_id"):
        return False

    creds = await refresh_user_credentials(user)
    loop = asyncio.get_running_loop()

    def _update():
        gc = gspread.authorize(creds)
        ws = gc.open_by_key(user["sheets_id"]).sheet1
        records = ws.get_all_records()
        pending = [
            (idx + 2, r)
            for idx, r in enumerate(records)
            if str(r.get("estado", "")).lower() == "pendiente"
        ]
        no_date = [(row, r) for row, r in pending if not str(r.get("fecha", "")).strip()]
        dated = sorted(
            [(row, r) for row, r in pending if str(r.get("fecha", "")).strip()],
            key=lambda x: str(x[1].get("fecha", "")),
            reverse=True,
        )
        sorted_pending = no_date + dated

        if posicion < 1 or posicion > len(sorted_pending):
            return False

        row_idx, _ = sorted_pending[posicion - 1]
        headers = ws.row_values(1)

        if nuevo_nombre:
            tarea_col = headers.index("tarea") + 1
            ws.update_cell(row_idx, tarea_col, nuevo_nombre)
        if nueva_fecha is not None:
            fecha_col = headers.index("fecha") + 1
            ws.update_cell(row_idx, fecha_col, nueva_fecha)

        return True

    return await loop.run_in_executor(None, _update)


async def delete_event(user: dict, event_id: str) -> bool:
    creds = await refresh_user_credentials(user)
    loop = asyncio.get_running_loop()

    def _delete():
        service = build("calendar", "v3", credentials=creds)
        service.events().delete(calendarId="primary", eventId=event_id).execute()
        return True

    return await loop.run_in_executor(None, _delete)


async def create_event(
    user: dict, nombre: str, fecha: str, hora: Optional[str] = None
) -> dict:
    creds = await refresh_user_credentials(user)
    loop = asyncio.get_running_loop()

    def _create():
        service = build("calendar", "v3", credentials=creds)

        if hora:
            start_dt = datetime.strptime(f"{fecha} {hora}", "%Y-%m-%d %H:%M")
            end_dt = start_dt + timedelta(hours=1)
            body = {
                "summary": nombre,
                "start": {
                    "dateTime": start_dt.strftime("%Y-%m-%dT%H:%M:00"),
                    "timeZone": "America/Argentina/Buenos_Aires",
                },
                "end": {
                    "dateTime": end_dt.strftime("%Y-%m-%dT%H:%M:00"),
                    "timeZone": "America/Argentina/Buenos_Aires",
                },
            }
        else:
            body = {
                "summary": nombre,
                "start": {"date": fecha},
                "end": {"date": fecha},
            }

        event = service.events().insert(calendarId="primary", body=body).execute()
        return {
            "id": event.get("id"),
            "nombre": event.get("summary"),
            "link": event.get("htmlLink"),
        }

    return await loop.run_in_executor(None, _create)


# ── Gmail ─────────────────────────────────────────────────────────────────────

def _parse_email_headers(msg_data: dict) -> dict:
    headers = {h["name"]: h["value"] for h in msg_data.get("payload", {}).get("headers", [])}
    return {
        "id": msg_data["id"],
        "asunto": headers.get("Subject", "(Sin asunto)"),
        "remitente": headers.get("From", ""),
        "fecha": headers.get("Date", ""),
        "snippet": msg_data.get("snippet", ""),
    }


async def search_emails(user: dict, query: str, max_results: int = 5) -> list[dict]:
    creds = await refresh_user_credentials(user)
    loop = asyncio.get_running_loop()

    def _search():
        try:
            service = build("gmail", "v1", credentials=creds)
            result = service.users().messages().list(
                userId="me", q=query, maxResults=max_results
            ).execute()
            messages = result.get("messages", [])
            emails = []
            for msg in messages:
                msg_data = service.users().messages().get(
                    userId="me", id=msg["id"], format="metadata",
                    metadataHeaders=["Subject", "From", "Date"],
                ).execute()
                emails.append(_parse_email_headers(msg_data))
            return emails
        except HttpError as e:
            if e.resp.status == 403:
                raise GmailPermissionError()
            raise

    return await loop.run_in_executor(None, _search)


async def send_email(user: dict, to: str, subject: str, body: str) -> bool:
    creds = await refresh_user_credentials(user)
    loop = asyncio.get_running_loop()

    def _send():
        try:
            service = build("gmail", "v1", credentials=creds)
            msg = MIMEText(body)
            msg["to"] = to
            msg["subject"] = subject
            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            service.users().messages().send(userId="me", body={"raw": raw}).execute()
            return True
        except HttpError as e:
            if e.resp.status == 403:
                raise GmailPermissionError()
            raise

    return await loop.run_in_executor(None, _send)


# ── Gastos ────────────────────────────────────────────────────────────────────

EXPENSE_HEADERS = ["fecha", "monto", "categoria", "descripcion", "medio_pago"]


def _parse_monto(value) -> float:
    """Parse a money value, tolerating Argentine formatting (1.500,50)."""
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


def _get_gastos_ws(sh):
    """Return the Gastos worksheet, creating it with headers if missing."""
    try:
        return sh.worksheet("Gastos")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="Gastos", rows=2000, cols=len(EXPENSE_HEADERS))
        ws.append_row(EXPENSE_HEADERS)
        return ws


async def add_expense(
    user: dict,
    monto: float,
    categoria: str,
    descripcion: str = "",
    fecha: str | None = None,
    medio_pago: str | None = None,
) -> bool:
    if not user.get("sheets_id"):
        return False

    creds = await refresh_user_credentials(user)
    fecha = fecha or datetime.now(ARGENTINA_TZ).strftime("%Y-%m-%d")
    loop = asyncio.get_running_loop()

    def _add():
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(user["sheets_id"])
        ws = _get_gastos_ws(sh)
        ws.append_row([fecha, monto, categoria, descripcion, medio_pago or ""])
        return True

    return await loop.run_in_executor(None, _add)


async def get_expenses(
    user: dict,
    desde: str | None = None,
    hasta: str | None = None,
    categoria: str | None = None,
) -> dict:
    """Sum expenses filtered by date range (YYYY-MM-DD) and/or category."""
    empty = {"total": 0, "count": 0, "por_categoria": {}, "gastos": []}
    if not user.get("sheets_id"):
        return empty

    creds = await refresh_user_credentials(user)
    loop = asyncio.get_running_loop()

    def _get():
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(user["sheets_id"])
        try:
            ws = sh.worksheet("Gastos")
        except gspread.WorksheetNotFound:
            return empty
        records = ws.get_all_records()
        filtered = []
        for r in records:
            f = str(r.get("fecha", "")).strip()
            if desde and f < desde:
                continue
            if hasta and f > hasta:
                continue
            if categoria and str(r.get("categoria", "")).lower() != categoria.lower():
                continue
            filtered.append(r)

        por_cat: dict[str, float] = {}
        total = 0.0
        for r in filtered:
            m = _parse_monto(r.get("monto"))
            total += m
            cat = str(r.get("categoria", "Otros")).strip() or "Otros"
            por_cat[cat] = por_cat.get(cat, 0.0) + m

        return {
            "total": total,
            "count": len(filtered),
            "por_categoria": por_cat,
            "gastos": filtered[-15:],
        }

    return await loop.run_in_executor(None, _get)


# ── Gmail watch helper ────────────────────────────────────────────────────────

async def get_emails_from_since(user: dict, email_address: str, since: datetime) -> list[dict]:
    """Get new emails from a specific address since a given timestamp."""
    creds = await refresh_user_credentials(user)
    loop = asyncio.get_running_loop()
    since_ts = int(since.timestamp())

    def _get():
        try:
            service = build("gmail", "v1", credentials=creds)
            result = service.users().messages().list(
                userId="me",
                q=f"from:{email_address} after:{since_ts}",
                maxResults=10,
            ).execute()
            messages = result.get("messages", [])
            emails = []
            for msg in messages:
                msg_data = service.users().messages().get(
                    userId="me", id=msg["id"], format="metadata",
                    metadataHeaders=["Subject", "From", "Date"],
                ).execute()
                emails.append(_parse_email_headers(msg_data))
            return emails
        except HttpError as e:
            if e.resp.status == 403:
                raise GmailPermissionError()
            raise

    return await loop.run_in_executor(None, _get)
