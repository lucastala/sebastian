"""Envío de emails transaccionales con Resend (https://resend.com).

Variables de entorno:
- RESEND_API_KEY: API key de Resend
- EMAIL_FROM: remitente, ej. "Sebastian <hola@chatsebastian.com>" (dominio verificado en Resend)
- BOT_URL: link al bot de Telegram (default https://t.me/sebastiandev_bot)
"""

import logging
import os

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

RESEND_API_KEY = os.getenv("RESEND_API_KEY")
EMAIL_FROM = os.getenv("EMAIL_FROM", "Sebastian <hola@chatsebastian.com>")
BOT_URL = os.getenv("BOT_URL", "https://t.me/sebastiandev_bot")


def _activation_html(codigo: str) -> str:
    return f"""\
<div style="font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:480px;margin:auto;
            padding:32px;color:#1a1a1a">
  <h1 style="color:#2d9cdb;margin:0 0 8px">¡Gracias por suscribirte a Sebastian! 🎉</h1>
  <p style="font-size:15px;line-height:1.5;color:#444">
    Tu pago se acreditó correctamente. Acá está tu código de activación:
  </p>
  <div style="background:#f0f7ff;border:1px dashed #2d9cdb;border-radius:12px;
              text-align:center;padding:20px;margin:20px 0">
    <span style="font-size:28px;font-weight:bold;letter-spacing:2px;color:#2d9cdb">{codigo}</span>
  </div>
  <p style="font-size:15px;line-height:1.5;color:#444">
    Para activarlo:
  </p>
  <ol style="font-size:15px;line-height:1.7;color:#444;padding-left:20px">
    <li>Abrí Sebastian en Telegram: <a href="{BOT_URL}" style="color:#2d9cdb">{BOT_URL}</a></li>
    <li>Escribí <b>/start</b> y mandá este código: <b>{codigo}</b></li>
    <li>Conectá tu cuenta de Google y ¡listo!</li>
  </ol>
  <p style="font-size:13px;color:#999;margin-top:28px">
    Si no reconocés esta compra, ignorá este mensaje.
  </p>
</div>"""


async def send_activation_code(email: str, codigo: str) -> bool:
    """Envía el código de activación al comprador. Devuelve True si se envió."""
    if not RESEND_API_KEY:
        logger.warning("RESEND_API_KEY no configurada; no se envía email")
        return False
    if not email or "@" not in email:
        logger.warning(f"Email inválido, no se envía código: {email!r}")
        return False
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": EMAIL_FROM,
                    "to": [email],
                    "subject": "Tu código de activación de Sebastian 🤖",
                    "html": _activation_html(codigo),
                },
                timeout=20,
            )
        if resp.status_code >= 400:
            logger.error(f"Resend send failed: {resp.status_code} {resp.text}")
            return False
        logger.info(f"Código enviado por email a {email}")
        return True
    except Exception as e:
        logger.error(f"Error sending activation email to {email}: {e}")
        return False
