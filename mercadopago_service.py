"""Integración con MercadoPago Suscripciones (preapproval).

Usa la API REST de MercadoPago vía httpx. Las credenciales salen del .env:
- MP_ACCESS_TOKEN: Access Token de producción

NOTA: el flujo de suscripciones (preapproval) de MercadoPago tiene requisitos que
conviene verificar en el sandbox de MP antes de cobrar en serio (payer_email,
card_token según el caso). Este módulo deja la estructura lista; si algún request
devuelve error, revisar el log y ajustar el body según la doc de MP.
"""

import logging
import os

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN")
MP_BASE = "https://api.mercadopago.com"

SUB_REASON = "Sebastian - Plan Mensual"
# Precio configurable: poné MP_SUB_PRICE=1 en el .env/Render para probar
SUB_PRICE = float(os.getenv("MP_SUB_PRICE", "250000"))
SUB_CURRENCY = "ARS"
# A dónde vuelve el usuario después de pagar
BACK_URL = os.getenv("MP_BACK_URL", "https://www.chatsebastian.com")


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


def _auto_recurring() -> dict:
    return {
        "frequency": 1,
        "frequency_type": "months",
        "transaction_amount": SUB_PRICE,
        "currency_id": SUB_CURRENCY,
    }


async def create_subscription_plan() -> dict | None:
    """Crea el plan de suscripción mensual (se hace una sola vez). Devuelve el JSON
    del plan (incluye 'id' e 'init_point') o None si falla."""
    body = {
        "reason": SUB_REASON,
        "auto_recurring": _auto_recurring(),
        "back_url": BACK_URL,
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{MP_BASE}/preapproval_plan", json=body, headers=_headers(), timeout=20
        )
    if resp.status_code >= 400:
        logger.error(f"MP create_subscription_plan failed: {resp.status_code} {resp.text}")
        return None
    return resp.json()


async def create_subscription_link(chat_id: int) -> str | None:
    """Crea una suscripción con external_reference = chat_id y devuelve el init_point
    (URL de checkout) para que ese usuario pague."""
    body = {
        "reason": SUB_REASON,
        "external_reference": str(chat_id),
        "auto_recurring": _auto_recurring(),
        "back_url": BACK_URL,
        "status": "pending",
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{MP_BASE}/preapproval", json=body, headers=_headers(), timeout=20
        )
    if resp.status_code >= 400:
        logger.error(f"MP create_subscription_link failed: {resp.status_code} {resp.text}")
        return None
    return resp.json().get("init_point")


async def verify_payment(payment_id: str) -> dict | None:
    """Consulta un pago por su id para verificar que sea real y su estado."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{MP_BASE}/v1/payments/{payment_id}", headers=_headers(), timeout=20
        )
    if resp.status_code >= 400:
        logger.error(f"MP verify_payment failed: {resp.status_code} {resp.text}")
        return None
    return resp.json()
