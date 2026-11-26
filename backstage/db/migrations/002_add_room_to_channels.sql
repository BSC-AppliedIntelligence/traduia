-- ===========================================
-- ADD LIVEKIT ROOM INFO TO CHANNELS
-- ===========================================
-- Run this in Supabase SQL Editor
-- This allows Stage to know which room to connect to for each channel
-- ===========================================

-- Add livekit_room_name column if not exists
DO $$ 
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'channels' AND column_name = 'livekit_room_name'
    ) THEN
        ALTER TABLE public.channels ADD COLUMN livekit_room_name TEXT;
        RAISE NOTICE 'Added livekit_room_name column to channels';
    END IF;
END $$;

-- Add livekit_url column if not exists (optional, for convenience)
DO $$ 
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'channels' AND column_name = 'livekit_url'
    ) THEN
        ALTER TABLE public.channels ADD COLUMN livekit_url TEXT;
        RAISE NOTICE 'Added livekit_url column to channels';
    END IF;
END $$;

-- Create index for faster lookups
CREATE INDEX IF NOT EXISTS idx_channels_room_name ON public.channels(livekit_room_name);

-- ===========================================
-- HELPER FUNCTION: Get channels with room info for Stage
-- ===========================================
CREATE OR REPLACE FUNCTION get_event_channels_for_stage(p_event_code TEXT)
RETURNS TABLE (
    channel_id UUID,
    channel_name TEXT,
    language TEXT,
    language_code TEXT,
    is_original BOOLEAN,
    status TEXT,
    livekit_room_name TEXT,
    livekit_url TEXT,
    track_name TEXT
) AS $$
BEGIN
    RETURN QUERY
    SELECT 
        c.id as channel_id,
        c.name as channel_name,
        c.language,
        c.language_code,
        c.is_original,
        c.status,
        c.livekit_room_name,
        c.livekit_url,
        c.track_name
    FROM public.channels c
    JOIN public.events e ON e.id = c.event_id
    WHERE e.event_code = p_event_code
    ORDER BY c.is_original DESC, c.language;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Grant execute permission
GRANT EXECUTE ON FUNCTION get_event_channels_for_stage(TEXT) TO anon;
GRANT EXECUTE ON FUNCTION get_event_channels_for_stage(TEXT) TO authenticated;

-- ===========================================
-- VERIFICATION
-- ===========================================
-- Check columns exist:
-- SELECT column_name, data_type 
-- FROM information_schema.columns 
-- WHERE table_name = 'channels';
--
-- Test function:
-- SELECT * FROM get_event_channels_for_stage('A12345');
-- ===========================================
