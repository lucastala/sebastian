# Sebastian SaaS — Asistente Personal en Telegram

Bot de Telegram multiusuario en Python. Cada usuario conecta su propia cuenta de Google y tiene su Calendar y su Google Sheet personal.

> Nota: el soporte de Gmail no está incluido en la v1 (se quitó para usar solo scopes sensibles y evitar la verificación restringida de Google). Podría sumarse en una v2.

---

## Qué puede hacer

Todo en lenguaje natural, sin comandos (salvo los atajos `.tarea` / `.N`):

- **Tareas** — agregar, listar, renombrar, cambiar fecha límite y eliminar.
- **Calendario** — crear, buscar, editar y eliminar eventos; avisa si dos se pisan (< 30 min).
- **Gastos** — registrar gastos con categoría automática y descripción; listar por período/categoría; editar el monto o eliminar.
- **Gastos fijos** — declarar gastos mensuales recurrentes (alquiler, seguro, suscripciones…) que se cargan solos cada mes.
- **Voz** — transcribe los audios con Whisper y los procesa como texto.

Las 10 categorías de gasto: Supermercado, Restaurantes y delivery, Transporte, Vivienda, Salud, Ropa y calzado, Suscripciones, Entretenimiento, Trabajo, Otros.

---

## Cómo funciona

El usuario le habla en lenguaje natural. El bot sigue esta lógica en orden:

| Entrada | Acción |
|---------|--------|
| `.1`, `.2`, `.N` | Elimina la tarea N de la lista. Sin pasar por OpenAI. |
| `.llamar al médico` | Agrega la tarea al Google Sheet. Sin pasar por OpenAI. |
| Audio de voz | Transcribe con Whisper → vuelve al inicio con el texto. |
| Cualquier otro texto | OpenAI gpt-4o-mini con function calling decide qué tool usar. |

Sebastian responde siempre tratando al usuario de **usted**, con tono cordial y profesional.

Al final de cualquier respuesta siempre aparece:
```
📋 Tareas pendientes:
1. tarea
2. tarea

Use .texto para agregar una tarea. Use .número para eliminar.
```

---

## Resumen diario automático — 8:00 AM Argentina

Para todos los usuarios con suscripción `activo` o `trial`:
```
☀️ ¡Buenos días! Este es su resumen de hoy:

📅 Eventos de hoy:
- 10:00 Reunión con el contador

📋 Tareas pendientes:
1. llamar al médico
2. pagar monotributo
```

Otros trabajos programados (`scheduler.py`):

| Job | Frecuencia | Qué hace |
|-----|-----------|----------|
| Resumen diario | 8:00 AR | Eventos + tareas del día |
| Gastos fijos | 9:00 AR | Carga los gastos fijos que vencen ese mes y avisa |

---

## Estructura de archivos

```
proyecto sebastian/
├── bot.py              ← lógica principal del bot
├── server.py           ← FastAPI para el callback de OAuth
├── database.py         ← funciones de Supabase
├── google_services.py  ← Sheets y Calendar por usuario
├── scheduler.py        ← resumen diario 8am, gastos fijos (9am), avisos de eventos
├── requirements.txt
├── schema.sql          ← SQL para crear la tabla en Supabase
├── .env                ← credenciales (no subir al repo)
├── .env.example        ← plantilla de variables
└── README.md
```

---

## Paso a paso para configurar

### 1. Crear proyecto en Supabase

1. Ir a [supabase.com](https://supabase.com) → New project
2. Dashboard → **SQL Editor** → New query
3. Pegar el contenido de `schema.sql` y ejecutar
4. Ir a **Settings → API**:
   - Copiar `Project URL` → `SUPABASE_URL`
   - Copiar `service_role` key (no la anon) → `SUPABASE_KEY`

### 2. Configurar Google OAuth en Google Cloud Console

1. Ir a [console.cloud.google.com](https://console.cloud.google.com)
2. Crear un proyecto nuevo (o usar uno existente)
3. **APIs & Services → Enable APIs**:
   - Google Calendar API ✓
   - Google Sheets API ✓
   - Google Drive API ✓
4. **APIs & Services → OAuth consent screen**:
   - User Type: **External**
   - Completar nombre de la app, email de soporte
   - Scopes: agregar `calendar`, `spreadsheets`, `drive.file`, `userinfo.email` (todos sensibles, sin scopes restringidos → verificación gratuita)
   - En **Test users**: agregar los emails que van a usar el bot durante desarrollo
5. **APIs & Services → Credentials → Create Credentials → OAuth client ID**:
   - Application type: **Web application**
   - Authorized redirect URIs: `http://localhost:8000/oauth/callback`
   - Guardar el **Client ID** → `GOOGLE_CLIENT_ID`
   - Guardar el **Client Secret** → `GOOGLE_CLIENT_SECRET`

### 3. Crear el bot en Telegram

1. Hablarle a [@BotFather](https://t.me/BotFather) en Telegram
2. `/newbot` → seguir los pasos
3. Copiar el token → `TELEGRAM_TOKEN`

### 4. Completar el `.env`

```bash
cp .env.example .env
```

Editar `.env` con todos los valores:

```env
TELEGRAM_TOKEN=7xxxxxxxxxx:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
OPENAI_API_KEY=sk-proj-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
SUPABASE_URL=https://xxxxxxxxxxxxxxxxxxxx.supabase.co
SUPABASE_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...   # service_role key
GOOGLE_CLIENT_ID=xxxxxxxxxxxx-xxxxxxxx.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-xxxxxxxxxxxxxxxxxxxxxxxx
GOOGLE_REDIRECT_URI=http://localhost:8000/oauth/callback
BASE_URL=http://localhost:8000
PAYMENT_LINK=https://tu-link-de-pago.com
```

### 5. Instalar dependencias

```bash
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # Mac/Linux

pip install -r requirements.txt
```

### 6. Correr en local

Necesitás **dos terminales** en paralelo:

**Terminal 1 — servidor OAuth:**
```bash
uvicorn server:app --reload --port 8000
```

**Terminal 2 — bot de Telegram:**
```bash
python bot.py
```

Ahora podés escribirle al bot. Al registrarte por primera vez el bot te manda un link `http://localhost:8000/oauth/start?chat_id=TU_ID`. Abrilo en el navegador, autorizás con tu cuenta de Google, y el bot queda listo.

---

## Tabla de Supabase — `usuarios`

| Columna | Tipo | Descripción |
|---------|------|-------------|
| `chat_id` | BIGINT PK | ID de Telegram |
| `email` | TEXT | Email de Google |
| `nombre` | TEXT | Nombre del usuario |
| `access_token` | TEXT | Token de Google OAuth |
| `refresh_token` | TEXT | Refresh token |
| `token_expiry` | TIMESTAMPTZ | Vencimiento del token |
| `sheets_id` | TEXT | ID del Google Sheet del usuario |
| `estado_suscripcion` | TEXT | `trial` / `activo` / `inactivo` |
| `genero` | TEXT | Trato preferido: `m` (señor) / `f` (señora) |
| `fecha_alta` | TIMESTAMPTZ | Fecha de registro |

> Importante: el backend usa la anon key, así que **RLS no se bypassa**. Toda tabla nueva necesita una política `FOR ALL USING (true) WITH CHECK (true)` y un `NOTIFY pgrst, 'reload schema';` tras crearla.

---

## Herramientas de OpenAI (function calling)

| Función | Descripción |
|---------|-------------|
| `get_today_events()` | Eventos de hoy en Google Calendar |
| `get_events_by_date(fecha)` | Eventos de una fecha específica |
| `search_event(query)` | Busca evento por nombre o descripción |
| `create_event(nombre, fecha, hora?)` | Crea evento; sin hora → todo el día; avisa si se pisa con otro |
| `update_event(event_id, ...)` | Edita nombre, fecha y/o hora de un evento |
| `delete_event(query, fecha?)` | Busca el evento y muestra botón de confirmación para eliminar |
| `get_pending_tasks()` | Lee tareas pendientes del Sheet |
| `update_task(posicion, ...)` | Renombra o cambia la fecha de una tarea por su número |
| `add_expense(monto, categoria, descripcion?, fecha?)` | Registra un gasto |
| `get_expenses(desde?, hasta?, categoria?)` | Suma y lista gastos por período/categoría |
| `update_expense_monto(posicion, nuevo_monto, ...)` | Cambia el monto de un gasto por su número |
| `delete_expense(posicion, ...)` | Elimina un gasto por su número |
| `add_fixed_expense(nombre, monto, categoria, dia_del_mes?)` | Declara un gasto fijo mensual |
| `get_fixed_expenses()` | Lista los gastos fijos activos |
| `cancel_fixed_expense(nombre)` | Da de baja un gasto fijo |
| `add_income / get_balance` | Ingresos y balance del período |
| `add_debt / get_debts / settle_debt` | Deudas (debo / me deben) |
| `add_super_item / get_super_list / remove_super_items / clear_super_list` | Lista de supermercado |

> Para operaciones de estado (eliminar, editar, gastos, etc.) el bot detecta la intención por palabras clave y **fuerza** el `tool_choice` correspondiente, porque el modelo no siempre llama estas funciones de forma confiable por sí solo.

---

## Datos por usuario en el Google Sheet

Cada usuario tiene un Google Sheet propio con tres pestañas, creadas automáticamente cuando se usan:

| Pestaña | Columnas |
|---------|----------|
| `Tareas` | id, tarea, estado, prioridad, fecha |
| `Gastos` | fecha, monto, categoria, descripcion, medio_pago |
| `GastosFijos` | nombre, monto, categoria, dia_del_mes, activo, ultimo_mes_cargado |

---

## Variables de entorno

| Variable | Descripción |
|----------|-------------|
| `TELEGRAM_TOKEN` | Token del bot de Telegram |
| `OPENAI_API_KEY` | API key de OpenAI |
| `SUPABASE_URL` | URL del proyecto Supabase |
| `SUPABASE_KEY` | Service role key de Supabase |
| `GOOGLE_CLIENT_ID` | Client ID de OAuth |
| `GOOGLE_CLIENT_SECRET` | Client Secret de OAuth |
| `GOOGLE_REDIRECT_URI` | URL de callback OAuth |
| `BASE_URL` | URL pública del servidor (para links de OAuth) |
| `PAYMENT_LINK` | Link al sistema de pago |
| `DAILY_SUMMARY_CHAT_ID` | chat_id del admin (habilita `/broadcast`) |

---

## Stack

| Componente | Tecnología |
|------------|------------|
| Bot | python-telegram-bot v21+ async |
| IA | OpenAI gpt-4.1-mini + function calling y visión |
| Voz | OpenAI Whisper |
| Datos del usuario | Google Sheets (gspread) — una hoja por usuario (pestañas Tareas, Gastos, GastosFijos, Ingresos, Deudas, Supermercado) |
| Calendario | Google Calendar API |
| Base de datos | Supabase (PostgreSQL) — tabla usuarios |
| Auth | Google OAuth 2.0 (google-auth-oauthlib) |
| Scheduler | APScheduler AsyncIOScheduler |
| Servidor OAuth | FastAPI + uvicorn |
