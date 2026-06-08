import asyncio
import base64
import hashlib
import logging
import os
import secrets

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from google_auth_oauthlib.flow import Flow

from database import update_user_sheet_id, update_user_tokens
from google_services import create_user_sheet

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
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

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
    <p>Tu cuenta de Google fue conectada exitosamente.</p>
    <p>Volvé a Telegram para empezar a usar el bot.</p>
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


@app.get("/oauth/start")
async def oauth_start(chat_id: int):
    code_verifier = _make_code_verifier()
    code_challenge = _make_code_challenge(code_verifier)

    flow = _build_flow()
    # Encode chat_id + code_verifier in state — survives server restarts
    state = f"{chat_id}:{code_verifier}"

    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        state=state,
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

    # Decode chat_id and code_verifier from state
    try:
        chat_id_str, code_verifier = state.split(":", 1)
        chat_id = int(chat_id_str)
    except (ValueError, AttributeError):
        return HTMLResponse(
            _ERROR_HTML.format(message="Estado inválido. Pedí el link de nuevo."),
            status_code=400,
        )

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
            _ERROR_HTML.format(message="No se pudo completar la autorización. Pedí el link de nuevo."),
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

    try:
        sheet_id = await create_user_sheet(credentials)
        await update_user_sheet_id(chat_id, sheet_id)
        logger.info(f"Created sheet {sheet_id} for chat_id={chat_id}")
    except Exception as e:
        logger.error(f"Failed to create sheet for chat_id={chat_id}: {e}")

    telegram_token = os.getenv("TELEGRAM_TOKEN")
    if telegram_token:
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"https://api.telegram.org/bot{telegram_token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": (
                            "✅ ¡Cuenta de Google conectada!\n\n"
                            "Ya podés usar tu asistente. Así funciona:\n\n"
                            "📝 .llamar al médico → agregar tarea\n"
                            "✅ .1 → eliminar tarea número 1\n"
                            "📅 'qué tengo hoy' → ver eventos\n"
                            "➕ 'reunión el viernes a las 10' → crear evento\n"
                            "🎤 Audio de voz → lo transcribo automáticamente\n\n"
                            "¡Estás listo!"
                        ),
                    },
                    timeout=10,
                )
        except Exception as e:
            logger.error(f"Failed to send Telegram notification to {chat_id}: {e}")

    return HTMLResponse(_SUCCESS_HTML)


@app.get("/health")
async def health():
    return {"status": "ok"}
