import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from telegram import Bot

from database import get_active_users, get_all_email_watches, get_user, update_watch_last_checked
from google_services import get_today_events, get_pending_tasks, get_emails_from_since, GmailPermissionError

logger = logging.getLogger(__name__)

ARGENTINA_TZ = "America/Argentina/Buenos_Aires"


async def send_daily_summary(bot: Bot) -> None:
    users = await get_active_users()
    logger.info(f"Sending daily summary to {len(users)} users")

    for user in users:
        if not user.get("access_token"):
            continue
        try:
            events = await get_today_events(user)
            tasks = await get_pending_tasks(user)

            if events:
                lines = ["📅 Eventos de hoy:"]
                for ev in events:
                    inicio = ev["inicio"]
                    if "T" in inicio:
                        hora = inicio.split("T")[1][:5]
                        lines.append(f"- {hora} {ev['nombre']}")
                    else:
                        lines.append(f"- Todo el día: {ev['nombre']}")
                events_block = "\n".join(lines)
            else:
                events_block = "📅 No tenés eventos para hoy."

            if tasks:
                task_lines = ["📋 Tareas pendientes:"]
                for i, t in enumerate(tasks, 1):
                    task_lines.append(f"{i}. {t['tarea']}")
                tasks_block = "\n".join(task_lines)
            else:
                tasks_block = "No tenés tareas pendientes."

            text = (
                f"☀️ Buenos días! Acá tu resumen de hoy:\n\n"
                f"{events_block}\n\n"
                f"{tasks_block}\n\n"
                f"Usá .texto para agregar tarea. Usá .número para eliminar."
            )

            await bot.send_message(chat_id=user["chat_id"], text=text)

        except Exception as e:
            logger.error(f"Error sending summary to user {user['chat_id']}: {e}")


async def check_email_watches(bot: Bot) -> None:
    watches = await get_all_email_watches()
    if not watches:
        return

    now = datetime.now(timezone.utc)
    logger.info(f"Checking {len(watches)} email watches")

    for watch in watches:
        chat_id = watch["chat_id"]
        email_address = watch["email_address"]
        last_checked_str = watch.get("last_checked", now.isoformat())
        try:
            last_checked = datetime.fromisoformat(last_checked_str.replace("Z", "+00:00"))
        except Exception:
            last_checked = now

        user = await get_user(chat_id)
        if not user or not user.get("access_token"):
            continue

        try:
            new_emails = await get_emails_from_since(user, email_address, last_checked)
            for mail in new_emails:
                text = (
                    f"📧 *Nuevo mail de {email_address}*\n\n"
                    f"*Asunto:* {mail['asunto']}\n"
                    f"*De:* {mail['remitente']}\n\n"
                    f"{mail['snippet']}"
                )
                await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
            await update_watch_last_checked(chat_id, email_address, now)
        except GmailPermissionError:
            logger.warning(f"No Gmail permission for user {chat_id}")
        except Exception as e:
            logger.error(f"Error checking emails for {chat_id} / {email_address}: {e}")


def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=ARGENTINA_TZ)
    scheduler.add_job(
        send_daily_summary,
        CronTrigger(hour=8, minute=0, timezone=ARGENTINA_TZ),
        args=[bot],
        id="daily_summary",
        name="Daily Morning Summary",
        replace_existing=True,
    )
    scheduler.add_job(
        check_email_watches,
        IntervalTrigger(minutes=5),
        args=[bot],
        id="email_watches",
        name="Email Watch Check",
        replace_existing=True,
    )
    return scheduler
