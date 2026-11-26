-- =====================================================
-- Link3rs - Whisper Config RPC Functions
-- =====================================================
-- Creates the RPC functions expected by bsc-backstage/main.py:
--   - public.get_whisper_config(p_event_code text)
--   - public.set_whisper_config(p_event_code text, p_config jsonb)
--
-- Run this in Supabase SQL Editor after the events.whisper_config
-- column exists.
-- =====================================================

CREATE OR REPLACE FUNCTION public.get_whisper_config(p_event_code TEXT)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    v_config JSONB;
BEGIN
    SELECT e.whisper_config
    INTO v_config
    FROM public.events e
    WHERE e.event_code = p_event_code
    LIMIT 1;

    RETURN COALESCE(
        v_config,
        '{"host": "", "port": 9090, "model": "small", "language": "ca", "translator_url": ""}'::jsonb
    );
END;
$$;

CREATE OR REPLACE FUNCTION public.set_whisper_config(p_event_code TEXT, p_config JSONB)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    v_config JSONB;
BEGIN
    UPDATE public.events
    SET whisper_config = COALESCE(p_config, '{}'::jsonb),
        updated_at = NOW()
    WHERE event_code = p_event_code;

    SELECT e.whisper_config
    INTO v_config
    FROM public.events e
    WHERE e.event_code = p_event_code
    LIMIT 1;

    RETURN v_config;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_whisper_config(TEXT) TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.set_whisper_config(TEXT, JSONB) TO anon, authenticated, service_role;

NOTIFY pgrst, 'reload schema';
