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

ALTER TABLE public.codigos_activacion ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role full access codigos"
    ON public.codigos_activacion
    FOR ALL
    USING (true)
    WITH CHECK (true);

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

CREATE POLICY "Service role full access recordatorios"
    ON public.recordatorios
    FOR ALL
    USING (true)
    WITH CHECK (true);

-- Enable Row Level Security
ALTER TABLE public.usuarios ENABLE ROW LEVEL SECURITY;

-- The bot uses the service-role key, so RLS is bypassed for the backend.
-- This policy is a safety net if the anon key is used by mistake.
CREATE POLICY "Service role full access"
    ON public.usuarios
    FOR ALL
    USING (true)
    WITH CHECK (true);
