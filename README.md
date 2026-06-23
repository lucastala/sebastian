# Sebastian SaaS — Asistente Personal en Telegram

Bot de Telegram multiusuario en Python. Asistente de productividad y finanzas
personales: tareas, gastos, listas, recordatorios y calendario, todo en lenguaje
natural. Modelo por suscripción (MercadoPago + códigos de activación).

La data del usuario (tareas, gastos, listas, etc.) vive en **Supabase**. Lo único
que se conecta a Google es el **Calendar**.

---

## Qué puede hacer

Todo en lenguaje natural (texto o audio 🎤), o con una foto 📷:

- **Tareas** — agregar, listar, renombrar, fecha límite, eliminar (atajos `.texto` / `.N`).
- **Recordatorios con hora** — "recordame X mañana a las 3" → avisa a esa hora.
- **Calendario (Google)** — crear, buscar, editar y eliminar eventos; avisa si dos se pisan y ~30 min antes de cada uno.
- **Gastos** — registrar con categoría automática y descripción; listar por período/categoría; editar el monto o eliminar. También por foto de ticket.
- **Gastos fijos** — recurrentes mensuales (alquiler, seguro…) que se cargan solos.
- **Ingresos y balance** — registrar cobros y ver el neto del mes.
- **Deudas** — lo que debe y lo que le deben, con totales.
- **Lista del súper y listados con nombre** — modo "dictado" para cargar de a muchos.
- **Resúmenes automáticos** — del día (8am) y del mes (día 1).

Sebastian responde tratando al usuario de **usted** (señor/señora configurable).

---

## Arquitectura

```
Telegram ──> bot.py (Background Worker)        ──> OpenAI (gpt-4.1-mini, Whisper, visión)
                 │                              ──> Supabase  (data del usuario)
                 │                              ──> Google Calendar
MercadoPago ──> server.py (Web Service, FastAPI)──> Resend (email del código)
                 (OAuth callback + webhook MP)  ──> Supabase
```

- **bot.py** — Background Worker (polling de Telegram).
- **server.py** — Web Service: callback de OAuth de Google + webhook de MercadoPago + endpoints de admin/pago.
- **Supabase (PostgreSQL)** — usuarios, códigos, recordatorios y toda la data (tareas, gastos, listas…).
- **Google** — solo Calendar (scopes: `openid`, `userinfo.email`, `calendar`).
- Hosting: Render (los dos servicios, siempre prendidos).

---

## Estructura de archivos

```
proyecto sebastian/
├── bot.py                  ← lógica del bot, tools de OpenAI, handlers
├── server.py               ← FastAPI: OAuth callback, webhook MP, endpoints admin/pago
├── data_store.py           ← acceso a datos en Supabase (tareas, gastos, listas, etc.)
├── google_services.py      ← auth + Google Calendar (lo único que queda en Google)
├── database.py             ← usuarios, suscripción, códigos, recordatorios (Supabase)
├── mercadopago_service.py  ← suscripción y verificación de pagos (MercadoPago)
├── email_service.py        ← envío del código de activación por email (Resend)
├── scheduler.py            ← jobs programados (APScheduler)
├── texts.py                ← textos compartidos (instrucciones/bienvenida)
├── migrate_to_supabase.py  ← script único: migró los Sheets viejos a Supabase
├── schema.sql              ← SQL de todas las tablas
├── requirements.txt
├── .env / .env.example
└── README.md
```

---

## Modelo de suscripción (MercadoPago)

1. Usuario nuevo escribe al bot → "ingresá tu código o suscribite en chatsebastian.com".
2. Paga la suscripción mensual (link de MercadoPago).
3. El **webhook** verifica el pago, genera un **código de activación** (`SEB-XXXXX`) y se lo **manda por email** (Resend).
4. El usuario escribe el código en el bot → queda **activo 30 días** → conecta Google → usa.
5. Pagos recurrentes extienden el vencimiento; un job diario marca **inactivos** a los vencidos.

Endpoints (en `server.py`, protegidos con `X-Admin-Key`):
- `POST /webhook/mercadopago` — notificaciones de pago.
- `POST /admin/crear-plan` — crea el plan y devuelve el link de suscripción.
- `POST /admin/generar-codigo` — genera códigos manualmente.
- `POST /admin/test-email` — manda un email de prueba.
- `GET /pago/suscripcion?chat_id=` — link de pago por usuario.

---

## Jobs programados (`scheduler.py`)

| Job | Frecuencia | Qué hace |
|-----|-----------|----------|
| Resumen diario | 8:00 AR | Eventos + tareas del día |
| Gastos fijos | 9:00 AR | Carga los fijos que vencen ese mes |
| Avisos de eventos | cada 10 min | Avisa ~30 min antes de cada evento |
| Recordatorios | cada 1 min | Dispara los recordatorios con hora |
| Resumen mensual | día 1, 10:00 AR | Gastos del mes anterior + comparación |
| Vencimiento de suscripción | 00:00 AR | Marca inactivos a los vencidos y avisa |

---

## Tablas de Supabase

`usuarios`, `codigos_activacion`, `recordatorios`, y la data del usuario:
`tareas`, `gastos`, `ingresos`, `gastos_fijos`, `deudas`, `supermercado`, `listados`.

El SQL completo está en `schema.sql`. Todas tienen `chat_id` (FK a `usuarios`) y RLS
con política permisiva (el backend usa la anon key, así que RLS no se bypassa).

`usuarios`: chat_id (PK), email, nombre, access_token, refresh_token, token_expiry,
estado_suscripcion (`trial`/`activo`/`inactivo`), fecha_vencimiento, genero, fecha_alta.

---

## Configurar Google OAuth (solo Calendar)

1. [console.cloud.google.com](https://console.cloud.google.com) → habilitar **Google Calendar API**.
2. **OAuth consent screen** → External → scopes: `openid`, `userinfo.email`, `calendar`
   (solo `calendar` es "sensible" → verificación gratuita, sin auditoría CASA).
3. **Credentials → OAuth client ID** (Web application) → redirect URI del callback.
4. Guardar `GOOGLE_CLIENT_ID` y `GOOGLE_CLIENT_SECRET`.

> Para producción hay que **publicar** la app (no dejarla en "Testing", donde el
> refresh token vence a los 7 días) y pasar la verificación de Google.

---

## Variables de entorno

| Variable | Descripción |
|----------|-------------|
| `TELEGRAM_TOKEN` | Token del bot |
| `OPENAI_API_KEY` | API key de OpenAI |
| `SUPABASE_URL` / `SUPABASE_KEY` | Proyecto Supabase (service_role key) |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `GOOGLE_REDIRECT_URI` | OAuth de Google |
| `BASE_URL` | URL pública del servidor OAuth |
| `MP_ACCESS_TOKEN` / `MP_PUBLIC_KEY` | MercadoPago (producción) |
| `MP_SUB_PRICE` | Precio de la suscripción (ej. 4900) |
| `MP_BACK_URL` / `SUBSCRIBE_URL` | URLs de la web |
| `ADMIN_API_KEY` | Protege los endpoints `/admin/*` |
| `RESEND_API_KEY` / `EMAIL_FROM` | Envío de emails (Resend, dominio verificado) |
| `DAILY_SUMMARY_CHAT_ID` | chat_id del admin (habilita `/broadcast`) |

---

## Stack

| Componente | Tecnología |
|------------|------------|
| Bot | python-telegram-bot v21+ async (polling) |
| IA | OpenAI gpt-4.1-mini (function calling + visión), Whisper |
| Datos del usuario | **Supabase (PostgreSQL)** |
| Calendario | Google Calendar API |
| Auth | Google OAuth 2.0 |
| Pagos | MercadoPago (suscripciones) |
| Email | Resend |
| Scheduler | APScheduler |
| Servidor | FastAPI + uvicorn |
| Hosting | Render |

> Nota: cada mensaje pasa por un bucle multironda de tools (el modelo puede
> encadenar acciones, ej. buscar un evento y editarlo, en el mismo turno). Para
> las operaciones de estado se fuerza el `tool_choice` por detección de intención.
