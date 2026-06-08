import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from telegram import Bot

from database import get_active_users, get_all_email_watches, get_user, update_watch_last_checked
from google_services import (
    GmailPermissionError,
    get_emails_from_since,
    get_pending_tasks,
    get_today_events,
    log_due_fixed_expenses,
)

ARG_TZ = timezone(timedelta(hours=-3))

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
                events_block = "📅 No tiene eventos para hoy."

            if tasks:
                task_lines = ["📋 Tareas pendientes:"]
                for i, t in enumerate(tasks, 1):
                    task_lines.append(f"{i}. {t['tarea']}")
                tasks_block = "\n".join(task_lines)
            else:
                tasks_block = "No tiene tareas pendientes."

            text = (
                f"☀️ ¡Buenos días! Este es su resumen de hoy:\n\n"
                f"{events_block}\n\n"
                f"{tasks_block}\n\n"
                f"Use .texto para agregar una tarea. Use .número para eliminar."
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
                header = (
                    f"📧 *Nuevo mail de {email_address}*\n\n"
                    f"*Asunto:* {mail['asunto']}\n"
                    f"*De:* {mail['remitente']}\n\n"
                )
                snippet = mail["snippet"]
                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=header + snippet,
                        parse_mode="Markdown",
                    )
                except Exception:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=f"📧 Nuevo mail de {email_address}\n\nAsunto: {mail['asunto']}\nDe: {mail['remitente']}\n\n{snippet}",
                    )
            await update_watch_last_checked(chat_id, email_address, now)
        except GmailPermissionError:
            logger.warning(f"No Gmail permission for user {chat_id}")
        except Exception as e:
            logger.error(f"Error checking emails for {chat_id} / {email_address}: {e}")


async def check_fixed_expenses(bot: Bot) -> None:
    users = await get_active_users()
    today = datetime.now(ARG_TZ)
    logger.info(f"Checking fixed expenses for {len(users)} users")

    for user in users:
        if not user.get("access_token"):
            continue
        try:
            logged = await log_due_fixed_expenses(user, today)
            if not logged:
                continue
            lines = ["💳 *Gastos fijos cargados este mes:*"]
            total = 0.0
            for g in logged:
                try:
                    total += float(g["monto"])
                except (ValueError, TypeError):
                    pass
                lines.append(f"• {g['nombre']}: ${g['monto']} ({g['categoria']})")
            lines.append(f"\n*Total:* ${total:,.0f}")
            text = "\n".join(lines)
            try:
                await bot.send_message(chat_id=user["chat_id"], text=text, parse_mode="Markdown")
            except Exception:
                await bot.send_message(chat_id=user["chat_id"], text=text)
        except Exception as e:
            logger.error(f"Error logging fixed expenses for {user['chat_id']}: {e}")


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
    scheduler.add_job(
        check_fixed_expenses,
        CronTrigger(hour=9, minute=0, timezone=ARGENTINA_TZ),
        args=[bot],
        id="fixed_expenses",
        name="Monthly Fixed Expenses Logging",
        replace_existing=True,
    )
    return scheduler
