-- Run this in the Supabase SQL editor (Dashboard → SQL Editor → New query)

CREATE TABLE IF NOT EXISTS public.usuarios (
    chat_id            BIGINT        PRIMARY KEY,
    email              TEXT,
    nombre             TEXT          NOT NULL,
    access_token       TEXT,
    refresh_token      TEXT,
    token_expiry       TIMESTAMPTZ,
    sheets_id          TEXT,
    genero             TEXT,
    estado_suscripcion TEXT          NOT NULL DEFAULT 'trial'
                           CHECK (estado_suscripcion IN ('trial', 'activo', 'inactivo')),
    fecha_alta         TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

-- Si la tabla ya existía, agregá la columna de trato (señor/señora):
ALTER TABLE public.usuarios ADD COLUMN IF NOT EXISTS genero TEXT;

-- Vencimiento de la suscripción (se setea a +30 días al activar)
ALTER TABLE public.usuarios ADD COLUMN IF NOT EXISTS fecha_vencimiento TIMESTAMPTZ;

-- Códigos de activación (los genera el webhook de MercadoPago o el admin)
CREATE TABLE IF NOT EXISTS public.codigos_activacion (
    id              BIGSERIAL    PRIMARY KEY,
    codigo          TEXT         NOT NULL UNIQUE,
    email_comprador TEXT,
    chat_id         BIGINT,
    estado          TEXT         NOT NULL DEFAULT 'sin_usar'
                        CHECK (estado IN ('sin_usar', 'usado')),
    fecha_creacion  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    fecha_uso       TIMESTAMPTZ,
    mp_payment_id   TEXT
);

-- RLS prendido y SIN políticas: el backend usa la SECRET key (sb_secret_/service_role),
-- que se saltea RLS. NO crear políticas USING(true): le abrirían la tabla a la llave
-- pública (sb_publishable_/anon) y expondrían los datos.
ALTER TABLE public.codigos_activacion ENABLE ROW LEVEL SECURITY;

-- Index for fast subscription lookups (used by the daily summary)
CREATE INDEX IF NOT EXISTS idx_usuarios_estado
    ON public.usuarios (estado_suscripcion);

-- Recordatorios con hora (los dispara el scheduler)
CREATE TABLE IF NOT EXISTS public.recordatorios (
    id          BIGSERIAL    PRIMARY KEY,
    chat_id     BIGINT       NOT NULL REFERENCES public.usuarios(chat_id) ON DELETE CASCADE,
    texto       TEXT         NOT NULL,
    fecha_hora  TIMESTAMPTZ  NOT NULL,
    enviado     BOOLEAN      NOT NULL DEFAULT FALSE,
    creado      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_recordatorios_pendientes
    ON public.recordatorios (fecha_hora) WHERE enviado = FALSE;

ALTER TABLE public.recordatorios ENABLE ROW LEVEL SECURITY;

-- RLS prendido y sin políticas (ver nota arriba): el backend usa la SECRET key
-- y se saltea RLS; la llave pública queda sin acceso.
ALTER TABLE public.usuarios ENABLE ROW LEVEL SECURITY;

-- ── OAuth flows ─────────────────────────────────────────────────────────────
-- Estado efímero del flujo OAuth. El bot inserta una fila con un token opaco
-- atado al chat_id; el servidor la consume y la borra (uso único). Así el
-- chat_id nunca viaja por la URL del navegador.
CREATE TABLE IF NOT EXISTS public.oauth_flows (
    token         TEXT          PRIMARY KEY,
    chat_id       BIGINT        NOT NULL,
    code_verifier TEXT,
    created_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

-- RLS prendido y SIN políticas (el backend usa la SECRET key, que bypassa RLS).
ALTER TABLE public.oauth_flows ENABLE ROW LEVEL SECURITY;

-- ── Pagos procesados (anti-replay del webhook de MercadoPago) ─────────────────
-- Guarda cada payment_id ya procesado para no generar códigos / extender de más
-- si MercadoPago (o un atacante) reenvía la misma notificación.
CREATE TABLE IF NOT EXISTS public.pagos_procesados (
    mp_payment_id TEXT         PRIMARY KEY,
    procesado_en  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

ALTER TABLE public.pagos_procesados ENABLE ROW LEVEL SECURITY;

-- ── Gastos: medio de pago + compras en cuotas ────────────────────────────────
-- Cómo se pagó cada gasto (efectivo/débito/crédito/transferencia/mercadopago).
ALTER TABLE public.gastos ADD COLUMN IF NOT EXISTS medio_pago TEXT;

-- Compras en cuotas con tarjeta (cada fila = una compra financiada en N cuotas).
CREATE TABLE IF NOT EXISTS public.cuotas (
    id           BIGSERIAL    PRIMARY KEY,
    chat_id      BIGINT       NOT NULL,
    descripcion  TEXT         NOT NULL,
    monto_cuota  NUMERIC      NOT NULL,
    total_cuotas INT          NOT NULL,
    categoria    TEXT,
    fecha_inicio DATE         NOT NULL,
    creado       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

ALTER TABLE public.cuotas ENABLE ROW LEVEL SECURITY;
