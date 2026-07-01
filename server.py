import asyncio
import base64
import hashlib
import logging
import os
import secrets
from datetime import datetime, timezone, timedelta

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from google_auth_oauthlib.flow import Flow

from database import (
    create_activation_code,
    delete_oauth_flow,
    extend_subscription_by_email,
    get_oauth_flow,
    mark_payment_processed,
    payment_already_processed,
    set_oauth_flow_verifier,
    update_user_tokens,
)
from email_service import send_activation_code
from mercadopago_service import (
    create_subscription_link,
    create_subscription_plan,
    verify_payment,
)
from texts import BIENVENIDA_CONECTADO

ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Sebastian SaaS — OAuth Server")

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/calendar",
]

OAUTH_FLOW_TTL = timedelta(minutes=15)

_SUCCESS_HTML = """
<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <title>Autorización completada</title>
  <style>
    body { font-family: -apple-system, sans-serif; text-align: center;
           padding: 60px 20px; background: #f0f2f5; }
    .card { background: white; border-radius: 16px; padding: 40px;
             max-width: 440px; margin: auto; box-shadow: 0 4px 20px rgba(0,0,0,.1); }
    h1 { color: #2d9cdb; } p { color: #555; }
  </style>
</head>
<body>
  <div class="card">
    <h1>✅ ¡Autorización completada!</h1>
    <p>Su cuenta de Google fue conectada exitosamente.</p>
    <p>Vuelva a Telegram para empezar a usar el bot.</p>
  </div>
</body>
</html>
"""

_ERROR_HTML = """
<!DOCTYPE html>
<html lang="es">
<head><meta charset="UTF-8"><title>Error</title></head>
<body style="font-family:sans-serif;text-align:center;padding:60px">
  <h1>❌ Error en la autorización</h1>
  <p>{message}</p>
</body>
</html>
"""


def _build_flow() -> Flow:
    return Flow.from_client_config(
        {
            "web": {
                "client_id": os.getenv("GOOGLE_CLIENT_ID"),
                "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
                "redirect_uris": [os.getenv("GOOGLE_REDIRECT_URI")],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=SCOPES,
        redirect_uri=os.getenv("GOOGLE_REDIRECT_URI"),
    )


def _make_code_verifier() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


def _make_code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _flow_expired(flow_row: dict) -> bool:
    created = datetime.fromisoformat(flow_row["created_at"])
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - created > OAUTH_FLOW_TTL


@app.get("/oauth/start")
async def oauth_start(token: str):
    flow_row = await get_oauth_flow(token)
    if not flow_row or _flow_expired(flow_row):
        if flow_row:
            await delete_oauth_flow(token)
        return HTMLResponse(
            _ERROR_HTML.format(
                message="Link inválido o expirado. Pedí uno nuevo desde Telegram."
            ),
            status_code=400,
        )

    code_verifier = _make_code_verifier()
    code_challenge = _make_code_challenge(code_verifier)
    await set_oauth_flow_verifier(token, code_verifier)

    flow = _build_flow()
    # El state es el token opaco; el chat_id vive server-side en oauth_flows.
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        state=token,
        prompt="consent",
        code_challenge=code_challenge,
        code_challenge_method="S256",
    )
    return RedirectResponse(auth_url)


@app.get("/oauth/callback")
async def oauth_callback(request: Request):
    code = request.query_params.get("code")
    state = request.query_params.get("state")

    if not code or not state:
        return HTMLResponse(
            _ERROR_HTML.format(message="Faltan parámetros en la respuesta de Google."),
            status_code=400,
        )

    # Resolve chat_id + code_verifier from the server-side flow (state = token).
    flow_row = await get_oauth_flow(state)
    if not flow_row or not flow_row.get("code_verifier") or _flow_expired(flow_row):
        logger.warning("OAuth callback con state inválido/expirado/incompleto")
        if flow_row:
            await delete_oauth_flow(state)
        return HTMLResponse(
            _ERROR_HTML.format(message="Estado inválido o expirado. Pida el enlace de nuevo."),
            status_code=400,
        )

    chat_id = flow_row["chat_id"]
    code_verifier = flow_row["code_verifier"]
    await delete_oauth_flow(state)  # uso único

    loop = asyncio.get_running_loop()
    try:
        flow = _build_flow()
        await loop.run_in_executor(
            None,
            lambda: flow.fetch_token(code=code, code_verifier=code_verifier),
        )
    except Exception as e:
        logger.error(f"Token exchange failed for chat_id={chat_id}: {e}")
        return HTMLResponse(
            _ERROR_HTML.format(message="No se pudo completar la autorización. Pida el enlace de nuevo."),
            status_code=500,
        )

    credentials = flow.credentials

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://www.googleapis.com/oauth2/v1/userinfo",
                headers={"Authorization": f"Bearer {credentials.token}"},
                timeout=10,
            )
            user_info = resp.json()
        email = user_info.get("email", "")
    except Exception as e:
        logger.error(f"Failed to fetch user info for chat_id={chat_id}: {e}")
        email = ""

    await update_user_tokens(
        chat_id=chat_id,
        access_token=credentials.token,
        refresh_token=credentials.refresh_token,
        token_expiry=credentials.expiry,
        email=email,
    )

    telegram_token = os.getenv("TELEGRAM_TOKEN")
    if telegram_token:
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"https://api.telegram.org/bot{telegram_token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": BIENVENIDA_CONECTADO,
                        "parse_mode": "Markdown",
                        "reply_markup": {"inline_keyboard": [[
                            {"text": "📖 Ver todo lo que puedo hacer", "callback_data": "menu_help"}
                        ]]},
                    },
                    timeout=10,
                )
        except Exception as e:
            logger.error(f"Failed to send Telegram notification to {chat_id}: {e}")

    return HTMLResponse(_SUCCESS_HTML)


@app.get("/health")
async def health():
    return {"status": "ok"}


# ── MercadoPago: webhook, admin y link de pago ────────────────────────────────

async def _notify_telegram(chat_id: int, text: str) -> None:
    token = os.getenv("TELEGRAM_TOKEN")
    if not token or not chat_id:
        return
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=10,
            )
    except Exception as e:
        logger.error(f"Failed to notify telegram {chat_id}: {e}")


@app.post("/webhook/mercadopago")
async def mercadopago_webhook(request: Request):
    """Recibe la notificación de MercadoPago, verifica el pago y, si está aprobado,
    genera un código de activación y se lo manda por email al comprador."""
    # MP manda el id del pago en el body (type=payment) o en query (?id=&topic=)
    payment_id = None
    try:
        body = await request.json()
        if body.get("type") == "payment" or body.get("action", "").startswith("payment"):
            payment_id = str(body.get("data", {}).get("id") or body.get("id") or "")
    except Exception:
        body = {}
    if not payment_id:
        qp = request.query_params
        if qp.get("topic") in ("payment", "merchant_order") or qp.get("type") == "payment":
            payment_id = qp.get("id") or qp.get("data.id")

    if not payment_id:
        # Otros eventos (suscripción creada, etc.) — los ignoramos pero respondemos OK
        return JSONResponse({"ok": True, "ignored": True})

    payment = await verify_payment(payment_id)
    if not payment:
        logger.error(f"Webhook: no se pudo verificar el pago {payment_id}")
        return JSONResponse({"ok": False}, status_code=200)

    status = payment.get("status")
    email = (payment.get("payer", {}) or {}).get("email", "")
    logger.info(f"Webhook MP pago {payment_id}: status={status} email={email}")

    if status != "approved":
        logger.info(f"Pago {payment_id} no aprobado ({status}), no se hace nada")
        return JSONResponse({"ok": True, "status": status})

    # Anti-replay: si ya procesamos este pago, no hacer nada (evita códigos/renovaciones de más).
    if await payment_already_processed(payment_id):
        logger.info(f"Pago {payment_id} ya procesado, se ignora (replay).")
        return JSONResponse({"ok": True, "duplicate": True})

    # Pago aprobado. ¿Es recurrente de un usuario ya activo? → extender vencimiento.
    extended_chat = await extend_subscription_by_email(email) if email else None
    if extended_chat:
        await mark_payment_processed(payment_id)
        logger.info(f"Suscripción extendida 30 días para chat_id={extended_chat} ({email})")
        await _notify_telegram(
            extended_chat, "✅ Recibimos tu pago. Tu suscripción se renovó por 30 días más. ¡Gracias!"
        )
        return JSONResponse({"ok": True, "renewed": True})

    # Pago nuevo → generar código de activación y mandarlo por email
    codigo = await create_activation_code(email or "desconocido", mp_payment_id=payment_id)
    if not codigo:
        logger.error(f"No se pudo generar código para el pago {payment_id}")
        return JSONResponse({"ok": False}, status_code=200)
    await mark_payment_processed(payment_id)

    logger.info(f"Código de activación generado para el pago {payment_id} (email={email})")
    # Enviar el código por email al comprador. NO lo devolvemos en la respuesta HTTP.
    enviado = await send_activation_code(email, codigo)
    return JSONResponse({"ok": True, "email_enviado": enviado})


@app.post("/admin/generar-codigo")
async def admin_generar_codigo(request: Request):
    """Genera códigos de activación manualmente. Requiere header X-Admin-Key."""
    if request.headers.get("X-Admin-Key") != ADMIN_API_KEY or not ADMIN_API_KEY:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    try:
        data = await request.json()
    except Exception:
        data = {}
    email = data.get("email", "")
    cantidad = int(data.get("cantidad", 1) or 1)
    cantidad = max(1, min(cantidad, 50))

    codigos = []
    for _ in range(cantidad):
        c = await create_activation_code(email or "manual")
        if c:
            codigos.append(c)
    return JSONResponse({"codigos": codigos})


@app.post("/admin/crear-plan")
async def admin_crear_plan(request: Request):
    """Crea el plan de suscripción (una vez) y devuelve el link público para suscribirse."""
    if request.headers.get("X-Admin-Key") != ADMIN_API_KEY or not ADMIN_API_KEY:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    plan = await create_subscription_plan()
    if not plan:
        return JSONResponse({"error": "no se pudo crear el plan (ver logs)"}, status_code=500)
    return JSONResponse({
        "id": plan.get("id"),
        "init_point": plan.get("init_point"),
        "sandbox_init_point": plan.get("sandbox_init_point"),
    })


@app.get("/pago/suscripcion")
async def pago_suscripcion(chat_id: int):
    """Genera un link de suscripción de MercadoPago para el usuario y lo redirige."""
    init_point = await create_subscription_link(chat_id)
    if not init_point:
        return HTMLResponse(
            _ERROR_HTML.format(message="No se pudo generar el link de pago. Intentá de nuevo."),
            status_code=500,
        )
    return RedirectResponse(init_point)
