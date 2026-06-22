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
