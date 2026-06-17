# Sebastian — Resumen para plan de verificación de Google OAuth

> Documento para pasarle a Claude y armar el plan de verificación de la app ante
> Google, con el objetivo de salir del modo "Testing" (solo usuarios de prueba) y
> poder abrir el bot a cualquier usuario. Foco en los puntos críticos de la
> verificación (sobre todo los scopes de Gmail).

## Qué es

Bot de Telegram multiusuario (asistente personal de productividad) en Python.
Cada usuario conecta **su propia** cuenta de Google vía OAuth 2.0. El bot
gestiona, sobre la cuenta del propio usuario:

- Tareas (Google Sheets, una hoja por usuario)
- Gastos, gastos fijos, ingresos y balance (mismo Sheet, pestañas extra)
- Eventos (Google Calendar)
- Correos (Gmail): buscar, enviar, y "vigilar" direcciones para avisar/resumir
- Voz (transcripción con Whisper) y fotos (visión: tickets, notas, eventos)

Idioma: español (Argentina). Trato formal de usted (configurable señor/señora).

## Arquitectura / hosting

- Lenguaje: Python 3.11
- Bot: python-telegram-bot v21 (async), polling (getUpdates)
- IA: OpenAI gpt-4o-mini (function calling + visión) y Whisper
- Datos de usuarios: Supabase (PostgreSQL) — tokens OAuth, email, sheets_id,
  estado de suscripción, género, y tabla `email_watches`
- Datos del usuario final: en **su propio** Google Drive (Sheet) y su
  Calendar/Gmail
- Hosting: Render
  - Servicio 1 `bot-tareas`: Background Worker (`python bot.py`)
  - Servicio 2 `sebastian-oauth`: Web Service FastAPI/uvicorn (callback OAuth)
- Repo: GitHub privado

## Flujo OAuth (actual)

1. Usuario hace `/start` en Telegram → recibe link a `/oauth/start?chat_id=...`
2. Redirige a Google OAuth (Authorization Code + PKCE)
3. Callback en `/oauth/callback` intercambia el code por tokens
4. Guarda `access_token` + `refresh_token` en Supabase
5. Crea un Google Sheet propio del usuario y lo deja listo

- `access_type=offline`, `prompt=consent` (para obtener refresh_token)
- Los tokens se refrescan con el refresh_token cuando expiran

## SCOPES solicitados (LO MÁS IMPORTANTE PARA LA VERIFICACIÓN)

| Scope | Clasificación |
|-------|---------------|
| `openid` | No sensible |
| `https://www.googleapis.com/auth/userinfo.email` | No sensible |
| `https://www.googleapis.com/auth/drive.file` | **No** sensible (recomendado) |
| `https://www.googleapis.com/auth/calendar` | **Sensible** |
| `https://www.googleapis.com/auth/spreadsheets` | **Sensible** |
| `https://www.googleapis.com/auth/gmail.readonly` | **Restringido** ⚠️ |
| `https://www.googleapis.com/auth/gmail.send` | **Restringido** ⚠️ |

Los dos scopes de Gmail son *restricted scopes* de Google → son los que disparan
el proceso de verificación más estricto (incluye, para producción con muchos
usuarios, una evaluación de seguridad de terceros **CASA**, anual y paga).

## Para qué se usa cada scope (justificación de uso)

- `userinfo.email`: identificar al usuario / asociar su cuenta
- `drive.file`: crear y editar **solo** el Sheet que el bot genera (no toca otros archivos)
- `spreadsheets`: leer/escribir tareas, gastos e ingresos en ese Sheet
- `calendar`: crear/leer/editar/eliminar eventos y avisar antes de cada uno
- `gmail.readonly`: buscar correos y vigilar remitentes para avisar/resumir
- `gmail.send`: enviar correos que el usuario redacta por lenguaje natural

## Estado actual

- App en modo "Testing" en Google Cloud → solo test users agregados a mano
- Funciona, pero limitado a ~100 test users y con pantalla de "app no verificada"
- Objetivo: publicar y verificar para abrir a cualquier usuario

## Manejo de datos (relevante para política de privacidad / seguridad)

- Tokens OAuth guardados en Supabase (Postgres). *(Aclarar: cifrado en reposo,
  acceso con service-role key, RLS activado.)*
- El contenido de correos/calendar/sheets **no** se almacena en nuestra base; se
  procesa en memoria y se manda a OpenAI para generar respuestas.
  **PUNTO CLAVE: la API de OpenAI es un subprocesador externo — hay que declararlo.**
- No se vende ni comparte data; uso solo para dar el servicio al usuario.

## Puntos que quiero que el plan resuelva / tenga en cuenta

1. Verificación de scopes **restringidos** de Gmail: requisitos exactos, costo y
   tiempo del security assessment CASA, y si aplica según cantidad de usuarios.
2. Si conviene **reducir/eliminar** los scopes de Gmail para evitar el assessment
   (p. ej. lanzar primero sin Gmail, solo Calendar+Sheets, que son "sensibles"
   pero no "restringidos" y tienen verificación mucho más liviana).
3. Requisitos de la OAuth consent screen: dominio verificado, homepage pública,
   política de privacidad, logo, demostración en video del uso de cada scope.
4. Que el uso de OpenAI como subprocesador no viole la *Limited Use* policy de
   Google para datos de Gmail (esto es delicado: Google restringe enviar datos
   de Gmail a terceros / modelos de IA — hay que verificar compatibilidad).
5. Pasos concretos y orden recomendado para pasar de Testing a Producción.
6. Alternativas si la verificación de Gmail no es viable (ej. que cada usuario
   use su propia API key, o quitar Gmail).

## Dato crítico a investigar sí o sí

La *Limited Use Requirements* de Google para scopes restringidos de Gmail suele
**prohibir** transferir datos de Gmail a modelos de IA de terceros para entrenar
o incluso procesar, salvo excepciones. Como el bot manda contenido de correos a
OpenAI, esto puede ser el **mayor bloqueante** de la verificación. Necesito saber
si es viable y bajo qué condiciones, o si hay que rediseñar esa parte.
