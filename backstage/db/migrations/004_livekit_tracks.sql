-- =====================================================
-- Link3rs - LiveKit Tracks Table
-- =====================================================
-- Ejecuta este script en Supabase Dashboard:
-- SQL Editor → New Query → Pegar → Run
-- =====================================================

-- =====================================================
-- 1. CREAR TABLA livekit_tracks
-- =====================================================

CREATE TABLE IF NOT EXISTS public.livekit_tracks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    -- Relaciones
    event_id UUID NOT NULL REFERENCES public.events(id) ON DELETE CASCADE,
    channel_id UUID REFERENCES public.channels(id) ON DELETE SET NULL,
    
    -- Información de la Room
    room_name TEXT NOT NULL,
    livekit_url TEXT NOT NULL,
    
    -- Información del Track
    track_sid TEXT,                           -- LiveKit track SID (TR_xxx) - se llena al publicar
    track_name TEXT NOT NULL,                 -- Nombre del track (ej: "tts-es", "tts-en")
    track_type TEXT NOT NULL DEFAULT 'audio', -- 'audio' o 'video'
    track_source TEXT DEFAULT 'agent',        -- 'microphone', 'screen', 'agent'
    
    -- Información del Participante
    participant_sid TEXT,                     -- LiveKit participant SID
    participant_identity TEXT NOT NULL,       -- Identidad (ej: "session-server-xxx")
    
    -- Metadata
    language TEXT NOT NULL,                   -- Idioma del track ('es', 'en', 'ca', 'original')
    is_agent_track BOOLEAN DEFAULT TRUE,      -- True si es track de agente TTS
    is_active BOOLEAN DEFAULT TRUE,           -- Si el track está activo
    
    -- Timestamps
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    
    -- Índices únicos
    UNIQUE(event_id, track_name)
);

-- =====================================================
-- 2. ÍNDICES
-- =====================================================

CREATE INDEX IF NOT EXISTS idx_livekit_tracks_event ON public.livekit_tracks(event_id);
CREATE INDEX IF NOT EXISTS idx_livekit_tracks_language ON public.livekit_tracks(language);
CREATE INDEX IF NOT EXISTS idx_livekit_tracks_active ON public.livekit_tracks(is_active);
CREATE INDEX IF NOT EXISTS idx_livekit_tracks_room ON public.livekit_tracks(room_name);

-- =====================================================
-- 3. TRIGGER PARA updated_at
-- =====================================================

CREATE OR REPLACE FUNCTION update_livekit_tracks_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_livekit_tracks_timestamp ON public.livekit_tracks;
CREATE TRIGGER trigger_livekit_tracks_timestamp
    BEFORE UPDATE ON public.livekit_tracks
    FOR EACH ROW
    EXECUTE FUNCTION update_livekit_tracks_timestamp();

-- =====================================================
-- 4. ROW LEVEL SECURITY
-- =====================================================

ALTER TABLE public.livekit_tracks ENABLE ROW LEVEL SECURITY;

-- Owner del evento puede ver y modificar sus tracks
DROP POLICY IF EXISTS "livekit_tracks_owner" ON public.livekit_tracks;
CREATE POLICY "livekit_tracks_owner" ON public.livekit_tracks
    FOR ALL TO authenticated
    USING (
        EXISTS (
            SELECT 1 FROM public.events 
            WHERE events.id = livekit_tracks.event_id 
            AND events.user_id = auth.uid()
        )
    );

-- Service role (backend) tiene acceso total
DROP POLICY IF EXISTS "livekit_tracks_service" ON public.livekit_tracks;
CREATE POLICY "livekit_tracks_service" ON public.livekit_tracks
    FOR ALL TO service_role
    USING (true)
    WITH CHECK (true);

-- Lectura pública para eventos activos (para servicios internos)
DROP POLICY IF EXISTS "livekit_tracks_public_read" ON public.livekit_tracks;
CREATE POLICY "livekit_tracks_public_read" ON public.livekit_tracks
    FOR SELECT TO anon
    USING (
        is_active = TRUE
        AND EXISTS (
            SELECT 1 FROM public.events 
            WHERE events.id = livekit_tracks.event_id 
            AND events.status IN ('active', 'streaming')
        )
    );

-- =====================================================
-- 5. FUNCIÓN PARA LIMPIAR TRACKS INACTIVOS
-- =====================================================

CREATE OR REPLACE FUNCTION cleanup_old_tracks()
RETURNS INTEGER AS $$
DECLARE
    deleted_count INTEGER;
BEGIN
    DELETE FROM public.livekit_tracks
    WHERE is_active = FALSE
    AND updated_at < NOW() - INTERVAL '24 hours';
    
    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    RETURN deleted_count;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- =====================================================
-- 6. VISTA PARA CONSULTAR TRACKS DE UN EVENTO
-- =====================================================

DROP VIEW IF EXISTS public.active_tracks_view;
CREATE VIEW public.active_tracks_view AS
SELECT 
    t.id,
    t.event_id,
    t.room_name,
    t.livekit_url,
    t.track_sid,
    t.track_name,
    t.track_type,
    t.language,
    t.participant_identity,
    t.is_active,
    t.created_at,
    e.title AS event_title,
    e.event_code,
    e.status AS event_status,
    c.name AS channel_name
FROM public.livekit_tracks t
JOIN public.events e ON t.event_id = e.id
LEFT JOIN public.channels c ON t.channel_id = c.id
WHERE t.is_active = TRUE
AND e.status IN ('active', 'streaming');

GRANT SELECT ON public.active_tracks_view TO authenticated;
GRANT SELECT ON public.active_tracks_view TO anon;

ALTER TABLE events 
ADD COLUMN IF NOT EXISTS whisper_config JSONB DEFAULT '{"host": "", "port": 9090, "model": "small", "language": "ca", "translator_url": ""}'::jsonb;

ALTER TABLE events ADD COLUMN IF NOT EXISTS whisper_config JSONB DEFAULT '{"host": "", "port": 9090, "model": "small", "language": "ca", "translator_url": ""}'::jsonb;
-- =====================================================
-- LISTO! Verifica con:
-- SELECT * FROM active_tracks_view;
-- =====================================================
