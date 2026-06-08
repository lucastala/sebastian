-- Run this in the Supabase SQL editor (Dashboard → SQL Editor → New query)

CREATE TABLE IF NOT EXISTS public.usuarios (
    chat_id            BIGINT        PRIMARY KEY,
    email              TEXT,
    nombre             TEXT          NOT NULL,
    access_token       TEXT,
    refresh_token      TEXT,
    token_expiry       TIMESTAMPTZ,
    sheets_id          TEXT,
    estado_suscripcion TEXT          NOT NULL DEFAULT 'trial'
                           CHECK (estado_suscripcion IN ('trial', 'activo', 'inactivo')),
    fecha_alta         TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

-- Index for fast subscription lookups (used by the daily summary)
CREATE INDEX IF NOT EXISTS idx_usuarios_estado
    ON public.usuarios (estado_suscripcion);

-- Email watches table
CREATE TABLE IF NOT EXISTS public.email_watches (
    id            BIGSERIAL    PRIMARY KEY,
    chat_id       BIGINT       NOT NULL REFERENCES public.usuarios(chat_id) ON DELETE CASCADE,
    email_address TEXT         NOT NULL,
    last_checked  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE(chat_id, email_address)
);

-- RLS for email_watches — mirror the usuarios policy so the backend can write
ALTER TABLE public.email_watches ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role full access watches"
    ON public.email_watches
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
