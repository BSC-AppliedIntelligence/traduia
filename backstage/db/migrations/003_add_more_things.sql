-- Añadir columna is_active si no existe
ALTER TABLE public.livekit_tracks ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE;

-- Añadir columnas que falten del nuevo schema
ALTER TABLE public.livekit_tracks ADD COLUMN IF NOT EXISTS room_name TEXT;
ALTER TABLE public.livekit_tracks ADD COLUMN IF NOT EXISTS livekit_url TEXT;
ALTER TABLE public.livekit_tracks ADD COLUMN IF NOT EXISTS is_agent_track BOOLEAN DEFAULT TRUE;

-- Sincronizar is_active con el status existente
UPDATE public.livekit_tracks SET is_active = (status = 'active') WHERE is_active IS NULL;