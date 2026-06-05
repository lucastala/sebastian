import os
import asyncio
import calendar as cal_module
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import gspread
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

load_dotenv()

logger = logging.getLogger(__name__)

ARGENTINA_TZ = timezone(timedelta(hours=-3))

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
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


async def delete_task_by_position(user: dict, position: int) -> bool:
    """Mark the nth pending task as completed (preserves history)."""
    if not user.get("sheets_id"):
        return False

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
        if position < 1 or position > len(pending):
            return False
        row_idx, _ = pending[position - 1]
        ws.update_cell(row_idx, 3, "completada")  # column 3 = estado
        return True

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
                    "nombre": ev.get("summary", "Sin título"),
                    "inicio": start_val,
                    "descripcion": ev.get("description", ""),
                }
            )
        return events

    return await loop.run_in_executor(None, _search)


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
