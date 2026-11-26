-- ===========================================
-- Link3rs Platform - Complete Database Schema
-- ===========================================
-- This script:
-- 1. DROPS ALL existing policies (clean slate)
-- 2. Creates/updates all tables for Backstage + Redirect
-- 3. Sets up proper RLS policies
-- 4. Tracks LiveKit rooms and audio tracks
-- ===========================================
-- Run this in Supabase SQL Editor (Dashboard > SQL Editor)
-- ===========================================

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ===========================================
-- STEP 1: DROP ALL EXISTING POLICIES
-- ===========================================
-- This ensures a clean slate for all tables

DO $$ 
DECLARE
    r RECORD;
    tables TEXT[] := ARRAY[
        'profiles', 'events', 'channels', 'event_stats',
        'livekit_rooms', 'livekit_tracks', 'event_logs',
        'redirects'
    ];
    t TEXT;
BEGIN
    FOREACH t IN ARRAY tables
    LOOP
        FOR r IN (
            SELECT policyname 
            FROM pg_policies 
            WHERE tablename = t AND schemaname = 'public'
        )
        LOOP
            EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', r.policyname, t);
            RAISE NOTICE 'Dropped policy: % on %', r.policyname, t;
        END LOOP;
    END LOOP;
END $$;

-- ===========================================
-- STEP 2: CREATE/UPDATE TABLES
-- ===========================================

-- -----------------------------
-- 2.1 PROFILES TABLE
-- -----------------------------
CREATE TABLE IF NOT EXISTS public.profiles (
    id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    email TEXT,
    full_name TEXT,
    is_approved BOOLEAN DEFAULT FALSE,
    is_admin BOOLEAN DEFAULT FALSE,
    approved_at TIMESTAMPTZ,
    approved_by UUID REFERENCES public.profiles(id),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_profiles_email ON public.profiles(email);
CREATE INDEX IF NOT EXISTS idx_profiles_is_approved ON public.profiles(is_approved);
CREATE INDEX IF NOT EXISTS idx_profiles_is_admin ON public.profiles(is_admin);

-- -----------------------------
-- 2.2 EVENTS TABLE
-- -----------------------------
CREATE TABLE IF NOT EXISTS public.events (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT,
    event_code TEXT UNIQUE NOT NULL,
    status TEXT DEFAULT 'active' CHECK (status IN ('active', 'streaming', 'paused', 'archived', 'deleted')),
    -- LiveKit integration
    livekit_room_name TEXT,
    livekit_room_sid TEXT,
    stream_started_at TIMESTAMPTZ,
    stream_ended_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Add columns if they don't exist (for existing tables)
DO $$ 
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'events' AND column_name = 'livekit_room_name') THEN
        ALTER TABLE public.events ADD COLUMN livekit_room_name TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'events' AND column_name = 'livekit_room_sid') THEN
        ALTER TABLE public.events ADD COLUMN livekit_room_sid TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'events' AND column_name = 'stream_started_at') THEN
        ALTER TABLE public.events ADD COLUMN stream_started_at TIMESTAMPTZ;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'events' AND column_name = 'stream_ended_at') THEN
        ALTER TABLE public.events ADD COLUMN stream_ended_at TIMESTAMPTZ;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_events_user_id ON public.events(user_id);
CREATE INDEX IF NOT EXISTS idx_events_event_code ON public.events(event_code);
CREATE INDEX IF NOT EXISTS idx_events_status ON public.events(status);
CREATE INDEX IF NOT EXISTS idx_events_livekit_room ON public.events(livekit_room_name);

-- -----------------------------
-- 2.3 CHANNELS TABLE
-- -----------------------------
CREATE TABLE IF NOT EXISTS public.channels (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    event_id UUID NOT NULL REFERENCES public.events(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT 'en',
    language_code TEXT NOT NULL DEFAULT 'en-US',
    is_original BOOLEAN DEFAULT FALSE,
    status TEXT DEFAULT 'inactive' CHECK (status IN ('active', 'inactive')),
    -- LiveKit track info (populated when streaming)
    track_sid TEXT,
    track_name TEXT,
    participant_identity TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

DO $$ 
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'channels' AND column_name = 'track_sid') THEN
        ALTER TABLE public.channels ADD COLUMN track_sid TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'channels' AND column_name = 'track_name') THEN
        ALTER TABLE public.channels ADD COLUMN track_name TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'channels' AND column_name = 'participant_identity') THEN
        ALTER TABLE public.channels ADD COLUMN participant_identity TEXT;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_channels_event_id ON public.channels(event_id);
CREATE INDEX IF NOT EXISTS idx_channels_is_original ON public.channels(is_original);
CREATE INDEX IF NOT EXISTS idx_channels_language ON public.channels(language);

-- -----------------------------
-- 2.4 LIVEKIT ROOMS TABLE
-- Tracks LiveKit room lifecycle
-- -----------------------------
CREATE TABLE IF NOT EXISTS public.livekit_rooms (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    event_id UUID NOT NULL REFERENCES public.events(id) ON DELETE CASCADE,
    room_name TEXT NOT NULL,
    room_sid TEXT,
    status TEXT DEFAULT 'created' CHECK (status IN ('created', 'active', 'closed')),
    participant_count INTEGER DEFAULT 0,
    max_participants INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_livekit_rooms_event ON public.livekit_rooms(event_id);
CREATE INDEX IF NOT EXISTS idx_livekit_rooms_name ON public.livekit_rooms(room_name);
CREATE INDEX IF NOT EXISTS idx_livekit_rooms_status ON public.livekit_rooms(status);

-- -----------------------------
-- 2.5 LIVEKIT TRACKS TABLE
-- Tracks all audio/video tracks per room
-- Essential for external services to know what tracks exist
-- -----------------------------
CREATE TABLE IF NOT EXISTS public.livekit_tracks (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    room_id UUID NOT NULL REFERENCES public.livekit_rooms(id) ON DELETE CASCADE,
    event_id UUID NOT NULL REFERENCES public.events(id) ON DELETE CASCADE,
    channel_id UUID REFERENCES public.channels(id) ON DELETE SET NULL,
    -- Track identification
    track_sid TEXT NOT NULL,
    track_name TEXT,
    track_type TEXT NOT NULL CHECK (track_type IN ('audio', 'video', 'data')),
    track_source TEXT CHECK (track_source IN ('microphone', 'camera', 'screen_share', 'agent', 'unknown')),
    -- Language info (for translation tracks)
    language TEXT,
    language_code TEXT,
    -- Participant info
    participant_identity TEXT,
    participant_sid TEXT,
    participant_name TEXT,
    is_agent BOOLEAN DEFAULT FALSE,
    is_original BOOLEAN DEFAULT FALSE,
    -- Status
    status TEXT DEFAULT 'active' CHECK (status IN ('active', 'muted', 'ended')),
    -- Timestamps
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    ended_at TIMESTAMPTZ,
    -- Metadata (JSON for flexibility)
    metadata JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_livekit_tracks_room ON public.livekit_tracks(room_id);
CREATE INDEX IF NOT EXISTS idx_livekit_tracks_event ON public.livekit_tracks(event_id);
CREATE INDEX IF NOT EXISTS idx_livekit_tracks_channel ON public.livekit_tracks(channel_id);
CREATE INDEX IF NOT EXISTS idx_livekit_tracks_sid ON public.livekit_tracks(track_sid);
CREATE INDEX IF NOT EXISTS idx_livekit_tracks_language ON public.livekit_tracks(language);
CREATE INDEX IF NOT EXISTS idx_livekit_tracks_status ON public.livekit_tracks(status);

-- -----------------------------
-- 2.6 EVENT LOGS TABLE
-- Stores logs from all providers for debugging
-- -----------------------------
CREATE TABLE IF NOT EXISTS public.event_logs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    event_id UUID NOT NULL REFERENCES public.events(id) ON DELETE CASCADE,
    level TEXT NOT NULL CHECK (level IN ('debug', 'info', 'success', 'warning', 'error')),
    source TEXT NOT NULL, -- 'livekit', 'soniox', 'google', 'groq', 'system'
    message TEXT NOT NULL,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_event_logs_event ON public.event_logs(event_id);
CREATE INDEX IF NOT EXISTS idx_event_logs_level ON public.event_logs(level);
CREATE INDEX IF NOT EXISTS idx_event_logs_source ON public.event_logs(source);
CREATE INDEX IF NOT EXISTS idx_event_logs_created ON public.event_logs(created_at DESC);

-- Partition-like cleanup: auto-delete logs older than 7 days
-- (Optional - uncomment if you want automatic cleanup)
-- CREATE OR REPLACE FUNCTION cleanup_old_logs() RETURNS trigger AS $$
-- BEGIN
--     DELETE FROM public.event_logs WHERE created_at < NOW() - INTERVAL '7 days';
--     RETURN NULL;
-- END;
-- $$ LANGUAGE plpgsql;

-- -----------------------------
-- 2.7 EVENT STATS TABLE
-- -----------------------------
CREATE TABLE IF NOT EXISTS public.event_stats (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    event_id UUID UNIQUE NOT NULL REFERENCES public.events(id) ON DELETE CASCADE,
    -- Listener stats
    listeners INTEGER DEFAULT 0,
    peak_listeners INTEGER DEFAULT 0,
    unique_listeners INTEGER DEFAULT 0,
    -- Duration
    duration_seconds INTEGER DEFAULT 0,
    total_audio_seconds INTEGER DEFAULT 0,
    -- Translation stats
    total_translations INTEGER DEFAULT 0,
    total_words_translated INTEGER DEFAULT 0,
    total_insights_generated INTEGER DEFAULT 0,
    -- Cost tracking
    fixed_cost DECIMAL(10,2) DEFAULT 0.00,
    variable_cost DECIMAL(10,2) DEFAULT 0.00,
    soniox_cost DECIMAL(10,4) DEFAULT 0.00,
    google_tts_cost DECIMAL(10,4) DEFAULT 0.00,
    groq_cost DECIMAL(10,4) DEFAULT 0.00,
    -- Timestamps
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

DO $$ 
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'event_stats' AND column_name = 'peak_listeners') THEN
        ALTER TABLE public.event_stats ADD COLUMN peak_listeners INTEGER DEFAULT 0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'event_stats' AND column_name = 'total_audio_seconds') THEN
        ALTER TABLE public.event_stats ADD COLUMN total_audio_seconds INTEGER DEFAULT 0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'event_stats' AND column_name = 'total_translations') THEN
        ALTER TABLE public.event_stats ADD COLUMN total_translations INTEGER DEFAULT 0;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_event_stats_event_id ON public.event_stats(event_id);

-- -----------------------------
-- 2.8 REDIRECTS TABLE (Redirect Manager service)
-- -----------------------------
CREATE TABLE IF NOT EXISTS public.redirects (
    id SERIAL PRIMARY KEY,
    slot INTEGER UNIQUE NOT NULL,
    reference TEXT DEFAULT '',
    destination TEXT DEFAULT '',
    click_count INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_redirects_slot ON public.redirects(slot);

-- ===========================================
-- STEP 3: TRIGGERS
-- ===========================================

-- Trigger function for updated_at
CREATE OR REPLACE FUNCTION public.update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Apply triggers
DROP TRIGGER IF EXISTS update_profiles_updated_at ON public.profiles;
CREATE TRIGGER update_profiles_updated_at
    BEFORE UPDATE ON public.profiles
    FOR EACH ROW EXECUTE FUNCTION public.update_updated_at();

DROP TRIGGER IF EXISTS update_events_updated_at ON public.events;
CREATE TRIGGER update_events_updated_at
    BEFORE UPDATE ON public.events
    FOR EACH ROW EXECUTE FUNCTION public.update_updated_at();

DROP TRIGGER IF EXISTS update_channels_updated_at ON public.channels;
CREATE TRIGGER update_channels_updated_at
    BEFORE UPDATE ON public.channels
    FOR EACH ROW EXECUTE FUNCTION public.update_updated_at();

DROP TRIGGER IF EXISTS update_livekit_rooms_updated_at ON public.livekit_rooms;
CREATE TRIGGER update_livekit_rooms_updated_at
    BEFORE UPDATE ON public.livekit_rooms
    FOR EACH ROW EXECUTE FUNCTION public.update_updated_at();

DROP TRIGGER IF EXISTS update_livekit_tracks_updated_at ON public.livekit_tracks;
CREATE TRIGGER update_livekit_tracks_updated_at
    BEFORE UPDATE ON public.livekit_tracks
    FOR EACH ROW EXECUTE FUNCTION public.update_updated_at();

DROP TRIGGER IF EXISTS update_event_stats_updated_at ON public.event_stats;
CREATE TRIGGER update_event_stats_updated_at
    BEFORE UPDATE ON public.event_stats
    FOR EACH ROW EXECUTE FUNCTION public.update_updated_at();

DROP TRIGGER IF EXISTS update_redirects_updated_at ON public.redirects;
CREATE TRIGGER update_redirects_updated_at
    BEFORE UPDATE ON public.redirects
    FOR EACH ROW EXECUTE FUNCTION public.update_updated_at();

-- Trigger to auto-create profile on signup
CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO public.profiles (id, email, full_name)
    VALUES (
        NEW.id,
        NEW.email,
        COALESCE(NEW.raw_user_meta_data->>'full_name', '')
    )
    ON CONFLICT (id) DO NOTHING;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
    AFTER INSERT ON auth.users
    FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();

-- ===========================================
-- STEP 4: ENABLE RLS
-- ===========================================

ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.channels ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.livekit_rooms ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.livekit_tracks ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.event_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.event_stats ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.redirects ENABLE ROW LEVEL SECURITY;

-- ===========================================
-- STEP 5: CREATE RLS POLICIES
-- ===========================================

-- -----------------------------
-- PROFILES POLICIES
-- -----------------------------
CREATE POLICY "profiles_select_authenticated" ON public.profiles
    FOR SELECT TO authenticated
    USING (true);

CREATE POLICY "profiles_insert_own" ON public.profiles
    FOR INSERT TO authenticated
    WITH CHECK (auth.uid() = id);

CREATE POLICY "profiles_update_own_or_admin" ON public.profiles
    FOR UPDATE TO authenticated
    USING (
        auth.uid() = id 
        OR EXISTS (SELECT 1 FROM public.profiles WHERE id = auth.uid() AND is_admin = true)
    );

-- -----------------------------
-- EVENTS POLICIES
-- -----------------------------
CREATE POLICY "events_select_own" ON public.events
    FOR SELECT TO authenticated
    USING (auth.uid() = user_id);

CREATE POLICY "events_insert_own" ON public.events
    FOR INSERT TO authenticated
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY "events_update_own" ON public.events
    FOR UPDATE TO authenticated
    USING (auth.uid() = user_id);

CREATE POLICY "events_delete_own" ON public.events
    FOR DELETE TO authenticated
    USING (auth.uid() = user_id);

-- Service role has full access (explicit policy as safety net)
CREATE POLICY "events_service_all" ON public.events
    FOR ALL TO service_role
    USING (true)
    WITH CHECK (true);

-- Public can view all events (needed for backend API which may use anon key)
CREATE POLICY "events_select_public" ON public.events
    FOR SELECT TO anon
    USING (true);

-- Anon can update events (needed for backend endpoints save via anon key)
CREATE POLICY "events_update_anon" ON public.events
    FOR UPDATE TO anon
    USING (true)
    WITH CHECK (true);

-- -----------------------------
-- CHANNELS POLICIES  
-- -----------------------------
CREATE POLICY "channels_all_own" ON public.channels
    FOR ALL TO authenticated
    USING (
        EXISTS (SELECT 1 FROM public.events WHERE events.id = channels.event_id AND events.user_id = auth.uid())
    )
    WITH CHECK (
        EXISTS (SELECT 1 FROM public.events WHERE events.id = channels.event_id AND events.user_id = auth.uid())
    );

CREATE POLICY "channels_select_public" ON public.channels
    FOR SELECT TO anon
    USING (
        EXISTS (SELECT 1 FROM public.events WHERE events.id = channels.event_id AND events.status IN ('active', 'streaming'))
    );

-- -----------------------------
-- LIVEKIT ROOMS POLICIES
-- -----------------------------
CREATE POLICY "livekit_rooms_all_own" ON public.livekit_rooms
    FOR ALL TO authenticated
    USING (
        EXISTS (SELECT 1 FROM public.events WHERE events.id = livekit_rooms.event_id AND events.user_id = auth.uid())
    )
    WITH CHECK (
        EXISTS (SELECT 1 FROM public.events WHERE events.id = livekit_rooms.event_id AND events.user_id = auth.uid())
    );

CREATE POLICY "livekit_rooms_select_public" ON public.livekit_rooms
    FOR SELECT TO anon
    USING (
        EXISTS (SELECT 1 FROM public.events WHERE events.id = livekit_rooms.event_id AND events.status IN ('active', 'streaming'))
    );

-- -----------------------------
-- LIVEKIT TRACKS POLICIES
-- -----------------------------
CREATE POLICY "livekit_tracks_all_own" ON public.livekit_tracks
    FOR ALL TO authenticated
    USING (
        EXISTS (SELECT 1 FROM public.events WHERE events.id = livekit_tracks.event_id AND events.user_id = auth.uid())
    )
    WITH CHECK (
        EXISTS (SELECT 1 FROM public.events WHERE events.id = livekit_tracks.event_id AND events.user_id = auth.uid())
    );

-- Public can view track info (external services need this to connect)
CREATE POLICY "livekit_tracks_select_public" ON public.livekit_tracks
    FOR SELECT TO anon
    USING (
        EXISTS (SELECT 1 FROM public.events WHERE events.id = livekit_tracks.event_id AND events.status IN ('active', 'streaming'))
    );

-- -----------------------------
-- EVENT LOGS POLICIES
-- -----------------------------
CREATE POLICY "event_logs_all_own" ON public.event_logs
    FOR ALL TO authenticated
    USING (
        EXISTS (SELECT 1 FROM public.events WHERE events.id = event_logs.event_id AND events.user_id = auth.uid())
    )
    WITH CHECK (
        EXISTS (SELECT 1 FROM public.events WHERE events.id = event_logs.event_id AND events.user_id = auth.uid())
    );

-- -----------------------------
-- EVENT STATS POLICIES
-- -----------------------------
CREATE POLICY "event_stats_all_own" ON public.event_stats
    FOR ALL TO authenticated
    USING (
        EXISTS (SELECT 1 FROM public.events WHERE events.id = event_stats.event_id AND events.user_id = auth.uid())
    )
    WITH CHECK (
        EXISTS (SELECT 1 FROM public.events WHERE events.id = event_stats.event_id AND events.user_id = auth.uid())
    );

-- -----------------------------
-- REDIRECTS POLICIES (Redirect Manager)
-- -----------------------------
-- Authenticated users can manage all redirects
CREATE POLICY "redirects_all_authenticated" ON public.redirects
    FOR ALL TO authenticated
    USING (true)
    WITH CHECK (true);

-- Public can read redirects (for redirect URLs to work)
CREATE POLICY "redirects_select_public" ON public.redirects
    FOR SELECT TO anon
    USING (true);

-- ===========================================
-- STEP 6: GRANT PERMISSIONS
-- ===========================================

GRANT USAGE ON SCHEMA public TO anon;
GRANT USAGE ON SCHEMA public TO authenticated;

-- Tables - anon (read only where allowed by RLS)
GRANT SELECT ON public.profiles TO anon;
GRANT SELECT ON public.events TO anon;
GRANT SELECT ON public.channels TO anon;
GRANT SELECT ON public.livekit_rooms TO anon;
GRANT SELECT ON public.livekit_tracks TO anon;
GRANT SELECT ON public.redirects TO anon;

-- Tables - authenticated (full access where allowed by RLS)
GRANT ALL ON public.profiles TO authenticated;
GRANT ALL ON public.events TO authenticated;
GRANT ALL ON public.channels TO authenticated;
GRANT ALL ON public.livekit_rooms TO authenticated;
GRANT ALL ON public.livekit_tracks TO authenticated;
GRANT ALL ON public.event_logs TO authenticated;
GRANT ALL ON public.event_stats TO authenticated;
GRANT ALL ON public.redirects TO authenticated;

-- Sequences
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO authenticated;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO anon;

-- ===========================================
-- STEP 7: HELPER FUNCTIONS
-- ===========================================

-- Function to get event tracks with channel info
CREATE OR REPLACE FUNCTION get_event_tracks(p_event_code TEXT)
RETURNS TABLE (
    track_sid TEXT,
    track_name TEXT,
    language TEXT,
    language_code TEXT,
    is_original BOOLEAN,
    is_agent BOOLEAN,
    participant_identity TEXT,
    status TEXT
) AS $$
BEGIN
    RETURN QUERY
    SELECT 
        t.track_sid,
        t.track_name,
        t.language,
        t.language_code,
        t.is_original,
        t.is_agent,
        t.participant_identity,
        t.status
    FROM public.livekit_tracks t
    JOIN public.events e ON e.id = t.event_id
    WHERE e.event_code = p_event_code
    AND t.status = 'active'
    ORDER BY t.is_original DESC, t.language;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Function to get room info by event code
CREATE OR REPLACE FUNCTION get_event_room(p_event_code TEXT)
RETURNS TABLE (
    room_name TEXT,
    room_sid TEXT,
    status TEXT,
    participant_count INTEGER
) AS $$
BEGIN
    RETURN QUERY
    SELECT 
        r.room_name,
        r.room_sid,
        r.status,
        r.participant_count
    FROM public.livekit_rooms r
    JOIN public.events e ON e.id = r.event_id
    WHERE e.event_code = p_event_code
    AND r.status IN ('created', 'active')
    ORDER BY r.created_at DESC
    LIMIT 1;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Grant execute on functions
GRANT EXECUTE ON FUNCTION get_event_tracks(TEXT) TO anon;
GRANT EXECUTE ON FUNCTION get_event_tracks(TEXT) TO authenticated;
GRANT EXECUTE ON FUNCTION get_event_room(TEXT) TO anon;
GRANT EXECUTE ON FUNCTION get_event_room(TEXT) TO authenticated;

-- ===========================================
-- VERIFICATION QUERIES
-- ===========================================
-- Run these to verify everything is set up correctly:
--
-- List all policies:
-- SELECT tablename, policyname, cmd FROM pg_policies WHERE schemaname = 'public' ORDER BY tablename, policyname;
--
-- List all tables:
-- SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' ORDER BY table_name;
--
-- Check columns in a table:
-- SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'livekit_tracks';
--
-- ===========================================

-- ===========================================
-- DONE!
-- ===========================================
-- 
-- NEXT STEPS:
-- 
-- 1. Create your first admin user:
--    a) Sign up through the app
--    b) Run:
--       UPDATE public.profiles
--       SET is_admin = TRUE, is_approved = TRUE, approved_at = NOW()
--       WHERE email = 'your@email.com';
--
-- 2. Update static/js/config.js with your Supabase credentials
--
-- 3. Configure .env with LiveKit, Soniox, Google credentials
--
-- ===========================================
-- 
-- For external services to connect to tracks:
-- 
-- SELECT * FROM get_event_tracks('A12345');
-- SELECT * FROM get_event_room('A12345');
--
-- These functions can be called by external services via
-- Supabase's RPC endpoint:
-- POST /rest/v1/rpc/get_event_tracks
-- Body: { "p_event_code": "A12345" }
--
-- ===========================================
