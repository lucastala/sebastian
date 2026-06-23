# Sebastian — Verificación de Google OAuth

Estado y pasos para verificar la app y salir del modo "Testing".

## Qué accede a Google (v1)

Tareas, gastos, listas, etc. viven en **Supabase**. A Google solo se accede para
el **Calendar**. Scopes solicitados:

| Scope | Clasificación |
|-------|---------------|
| `openid` | No sensible |
| `https://www.googleapis.com/auth/userinfo.email` | No sensible |
| `https://www.googleapis.com/auth/calendar` | **Sensible** |

- **No hay scopes restringidos** (Gmail y Drive quedaron afuera) → **verificación gratuita**, sin auditoría CASA, ~3-5 días hábiles.
- Solo `calendar` es "sensible", lo que mantiene la verificación liviana.

## Por qué urge publicar (salir de Testing)

En modo **Testing**, Google **vence el refresh token a los 7 días**. A escala, eso
significa que cada usuario tendría que reconectar su cuenta cada semana. Publicar a
producción elimina ese vencimiento (aunque el cartel de "app no verificada" se
mantiene hasta completar la verificación).

## Paso a paso (Google Cloud Console)

1. **APIs habilitadas**: dejar solo **Google Calendar API**. Se pueden deshabilitar
   Sheets API y Drive API (ya no se usan).

2. **OAuth consent screen → Edit app**:
   - User type: **External**.
   - App name, logo, email de soporte, **developer contact email**.
   - **Authorized domains**: `chatsebastian.com`.
   - **App homepage**: `https://www.chatsebastian.com`.
   - **Privacy policy**: `https://www.chatsebastian.com/privacidad` (debe existir y ser pública).
   - **Terms of service**: `https://www.chatsebastian.com/terminos`.

3. **Scopes**: dejar **solo** `openid`, `.../auth/userinfo.email`, `.../auth/calendar`.
   **Quitar** `spreadsheets` y `drive.file` (ya no se piden en el código).

4. **Publishing status → Publish app** (pasar de Testing a "In production").

5. **Submit for verification**:
   - Como hay un scope sensible (`calendar`), Google pide justificar el uso y casi
     siempre un **video** mostrando: la pantalla de consentimiento (con los scopes),
     y cómo la app usa el Calendar (crear/ver/editar eventos desde el bot).
   - Completar el formulario de verificación y enviar.

6. **Esperar la aprobación** (días hábiles). Mientras tanto, en producción los
   usuarios ya no sufren el vencimiento de 7 días, aunque vean el aviso de no
   verificada hasta que Google apruebe.

## Datos para el formulario de verificación

- **Qué dato se accede**: eventos del Google Calendar del usuario.
- **Para qué**: el usuario gestiona su agenda por chat (crear, ver, editar,
  eliminar eventos y recibir recordatorios).
- **Dónde se guarda**: los eventos NO se almacenan en nuestra base; se leen/escriben
  en vivo contra Calendar. En Supabase guardamos tokens, datos del usuario y su
  data de productividad (tareas/gastos/etc.), no contenido de Calendar.
- **Subprocesadores**: OpenAI (procesa los mensajes), Supabase (base de datos),
  Render (hosting), MercadoPago (pagos), Resend (emails), Telegram.
