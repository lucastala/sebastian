"""Integración con Google Calendar (lo único que queda en Google).

Tareas, gastos, listas, etc. ahora viven en Supabase (ver data_store.py).
Acá quedan solo la autenticación y el Calendar.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


class GoogleAuthExpiredError(Exception):
    """The user's Google session expired/was revoked — they must re-authorize."""
    pass


load_dotenv()

logger = logging.getLogger(__name__)

ARGENTINA_TZ = timezone(timedelta(hours=-3))

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/calendar",
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
        try:
            await loop.run_in_executor(None, lambda: creds.refresh(Request()))
        except RefreshError as e:
            # Refresh token expired or revoked (e.g. 7-day limit in Testing mode)
            raise GoogleAuthExpiredError() from e

        from database import update_user_tokens

        await update_user_tokens(
            chat_id=user["chat_id"],
            access_token=creds.token,
            refresh_token=creds.refresh_token,
            token_expiry=creds.expiry,
            email=user.get("email", ""),
        )

    return creds


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


async def delete_event(user: dict, event_id: str) -> bool:
    creds = await refresh_user_credentials(user)
    loop = asyncio.get_running_loop()

    def _delete():
        service = build("calendar", "v3", credentials=creds)
        service.events().delete(calendarId="primary", eventId=event_id).execute()
        return True

    return await loop.run_in_executor(None, _delete)


async def create_event(
    user: dict,
    nombre: str,
    fecha: str,
    hora: Optional[str] = None,
    hora_fin: Optional[str] = None,
    duracion_min: Optional[int] = None,
) -> dict:
    creds = await refresh_user_credentials(user)
    loop = asyncio.get_running_loop()

    def _create():
        service = build("calendar", "v3", credentials=creds)

        if hora:
            start_dt = datetime.strptime(f"{fecha} {hora}", "%Y-%m-%d %H:%M")
            # Fin: hora_fin explícita (si es válida y posterior), o duración, o 1 hora.
            end_dt = None
            if hora_fin:
                try:
                    cand = datetime.strptime(f"{fecha} {hora_fin}", "%Y-%m-%d %H:%M")
                    if cand > start_dt:
                        end_dt = cand
                except ValueError:
                    end_dt = None
            if end_dt is None and duracion_min:
                end_dt = start_dt + timedelta(minutes=int(duracion_min))
            if end_dt is None:
                end_dt = start_dt + timedelta(hours=1)

            tz = "America/Argentina/Buenos_Aires"
            body = {
                "summary": nombre,
                "start": {"dateTime": start_dt.strftime("%Y-%m-%dT%H:%M:00"), "timeZone": tz},
                "end": {"dateTime": end_dt.strftime("%Y-%m-%dT%H:%M:00"), "timeZone": tz},
            }
        else:
            body = {
                "summary": nombre,
                "start": {"date": fecha},
                "end": {"date": fecha},
            }

        event = service.events().insert(calendarId="primary", body=body).execute()
        start_val = event["start"].get("dateTime", event["start"].get("date", ""))
        end_val = event["end"].get("dateTime", event["end"].get("date", ""))
        return {
            "id": event.get("id"),
            "nombre": event.get("summary"),
            "link": event.get("htmlLink"),
            "inicio": start_val,
            "fin": end_val,
            "all_day": "dateTime" not in event.get("start", {}),
        }

    return await loop.run_in_executor(None, _create)
