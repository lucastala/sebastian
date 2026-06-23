import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from telegram import Bot

from database import (
    get_active_users,
    get_due_reminders,
    get_expired_active_users,
    mark_reminder_sent,
    set_user_inactive,
)
from google_services import (
    get_balance,
    get_expenses,
    get_pending_tasks,
    get_today_events,
    log_due_fixed_expenses,
)

ARG_TZ = timezone(timedelta(hours=-3))

logger = logging.getLogger(__name__)

ARGENTINA_TZ = "America/Argentina/Buenos_Aires"

_MESES = ["", "enero", "febrero", "marzo", "abril", "mayo", "junio",
          "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
_CAT_EMOJI = {
    "Supermercado": "🛒", "Restaurantes y delivery": "🍕", "Transporte": "⛽",
    "Vivienda": "🏠", "Salud": "💊", "Ropa y calzado": "👕", "Suscripciones": "📱",
    "Entretenimiento": "🎉", "Trabajo": "💼", "Otros": "📦",
}


def _money(value) -> str:
    try:
        return f"${int(round(float(value))):,}".replace(",", ".")
    except (ValueError, TypeError):
        return f"${value}"

# Events already notified this run — (chat_id, event_id). Cleared on restart.
_notified_events: set[tuple[int, str]] = set()
EVENT_REMINDER_MINUTES = 30


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


async def check_upcoming_events(bot: Bot) -> None:
    users = await get_active_users()
    now = datetime.now(ARG_TZ)

    for user in users:
        if not user.get("access_token"):
            continue
        chat_id = user["chat_id"]
        try:
            events = await get_today_events(user)
        except Exception as e:
            logger.error(f"Error fetching events for reminders {chat_id}: {e}")
            continue

        for ev in events:
            inicio = ev.get("inicio", "")
            if "T" not in inicio:
                continue  # all-day event, no time reminder
            event_id = ev.get("id", "")
            key = (chat_id, event_id)
            if key in _notified_events:
                continue
            try:
                start_dt = datetime.fromisoformat(inicio)
            except ValueError:
                continue
            minutes_left = (start_dt - now).total_seconds() / 60
            if 0 < minutes_left <= EVENT_REMINDER_MINUTES:
                hora = inicio.split("T")[1][:5]
                text = (
                    f"⏰ En ~{int(round(minutes_left))} min: *{ev['nombre']}* ({hora})"
                )
                try:
                    await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
                except Exception:
                    await bot.send_message(chat_id=chat_id, text=f"⏰ En ~{int(round(minutes_left))} min: {ev['nombre']} ({hora})")
                _notified_events.add(key)


async def send_monthly_summary(bot: Bot) -> None:
    """On the 1st of the month, send each user a recap of the previous month."""
    users = await get_active_users()
    today = datetime.now(ARG_TZ)
    first_this = today.replace(day=1)
    last_prev = first_this - timedelta(days=1)        # last day of previous month
    first_prev = last_prev.replace(day=1)
    last_pp = first_prev - timedelta(days=1)           # month before that
    first_pp = last_pp.replace(day=1)

    desde_prev, hasta_prev = first_prev.strftime("%Y-%m-%d"), last_prev.strftime("%Y-%m-%d")
    desde_pp, hasta_pp = first_pp.strftime("%Y-%m-%d"), last_pp.strftime("%Y-%m-%d")
    mes_nombre = _MESES[last_prev.month]

    logger.info(f"Sending monthly summary ({mes_nombre}) to {len(users)} users")

    for user in users:
        if not user.get("access_token"):
            continue
        try:
            exp = await get_expenses(user, desde=desde_prev, hasta=hasta_prev)
            total = exp.get("total", 0)
            if total <= 0:
                continue  # nothing to report
            prev = await get_expenses(user, desde=desde_pp, hasta=hasta_pp)
            bal = await get_balance(user, desde=desde_prev, hasta=hasta_prev)

            lines = [f"📊 *Resumen de {mes_nombre}*\n", f"Gastaste *{_money(total)}*"]
            prev_total = prev.get("total", 0)
            if prev_total > 0:
                diff = (total - prev_total) / prev_total * 100
                signo = "más" if diff >= 0 else "menos"
                lines[-1] += f" ({abs(diff):.0f}% {signo} que {_MESES[last_pp.month]})."
            else:
                lines[-1] += "."

            por_cat = exp.get("por_categoria", {})
            if por_cat:
                lines.append("\nPor categoría:")
                for cat, monto in sorted(por_cat.items(), key=lambda x: x[1], reverse=True):
                    emoji = _CAT_EMOJI.get(cat, "•")
                    lines.append(f"{emoji} {cat}: {_money(monto)}")

            ingresos = bal.get("ingresos", 0)
            if ingresos > 0:
                lines.append(f"\n⬆️ Ingresos: {_money(ingresos)}  ·  🟢 Balance: {_money(bal.get('balance', 0))}")

            text = "\n".join(lines)
            try:
                await bot.send_message(chat_id=user["chat_id"], text=text, parse_mode="Markdown")
            except Exception:
                await bot.send_message(chat_id=user["chat_id"], text=text)
        except Exception as e:
            logger.error(f"Error sending monthly summary to {user['chat_id']}: {e}")


async def check_expired_subscriptions(bot: Bot) -> None:
    """Marca como inactivos a los usuarios cuya suscripción venció y les avisa."""
    expired = await get_expired_active_users()
    if not expired:
        return
    logger.info(f"Expiring {len(expired)} subscriptions")
    for user in expired:
        chat_id = user["chat_id"]
        try:
            await set_user_inactive(chat_id)
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    "Tu suscripción venció. Renovála en https://www.chatsebastian.com "
                    "o ingresá un nuevo código de activación."
                ),
            )
        except Exception as e:
            logger.error(f"Error expiring subscription for {chat_id}: {e}")


async def check_reminders(bot: Bot) -> None:
    due = await get_due_reminders()
    for r in due:
        try:
            await bot.send_message(
                chat_id=r["chat_id"], text=f"⏰ Recordatorio: {r['texto']}"
            )
        except Exception as e:
            logger.error(f"Error sending reminder {r.get('id')}: {e}")
        finally:
            await mark_reminder_sent(r["id"])


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
        check_fixed_expenses,
        CronTrigger(hour=9, minute=0, timezone=ARGENTINA_TZ),
        args=[bot],
        id="fixed_expenses",
        name="Monthly Fixed Expenses Logging",
        replace_existing=True,
    )
    scheduler.add_job(
        check_upcoming_events,
        IntervalTrigger(minutes=10),
        args=[bot],
        id="event_reminders",
        name="Upcoming Event Reminders",
        replace_existing=True,
    )
    scheduler.add_job(
        check_reminders,
        IntervalTrigger(minutes=1),
        args=[bot],
        id="reminders",
        name="Timed Reminders",
        replace_existing=True,
    )
    scheduler.add_job(
        send_monthly_summary,
        CronTrigger(day=1, hour=10, minute=0, timezone=ARGENTINA_TZ),
        args=[bot],
        id="monthly_summary",
        name="Monthly Expense Summary",
        replace_existing=True,
    )
    scheduler.add_job(
        check_expired_subscriptions,
        CronTrigger(hour=0, minute=0, timezone=ARGENTINA_TZ),
        args=[bot],
        id="expired_subscriptions",
        name="Expire Subscriptions",
        replace_existing=True,
    )
    return scheduler
