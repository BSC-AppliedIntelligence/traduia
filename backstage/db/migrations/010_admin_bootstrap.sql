-- Admin bootstrap without a hardcoded user.
-- Emails listed in public.bootstrap_admin_emails (filled from the ADMIN_EMAILS stack env by
-- the bootstrap job, see bootstrap/apply-admins.sql) are approved and made admin automatically
-- when their profile is created at sign-up. Everyone else stays pending approval.
-- Idempotent.

CREATE TABLE IF NOT EXISTS public.bootstrap_admin_emails (
    email TEXT PRIMARY KEY
);
ALTER TABLE public.bootstrap_admin_emails ENABLE ROW LEVEL SECURITY;  -- no policies: invisible to API roles
REVOKE ALL ON public.bootstrap_admin_emails FROM PUBLIC, anon, authenticated;

CREATE OR REPLACE FUNCTION public.is_bootstrap_admin(p_email TEXT)
RETURNS BOOLEAN
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public
AS $$
    SELECT EXISTS (
        SELECT 1 FROM public.bootstrap_admin_emails
        WHERE email = lower(btrim(coalesce(p_email, '')))
    );
$$;
REVOKE ALL ON FUNCTION public.is_bootstrap_admin(TEXT) FROM PUBLIC, anon, authenticated;

CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER AS $$
DECLARE
    v_admin BOOLEAN := public.is_bootstrap_admin(NEW.email);
BEGIN
    INSERT INTO public.profiles (id, email, full_name, is_approved, is_admin, approved_at)
    VALUES (
        NEW.id,
        NEW.email,
        COALESCE(NEW.raw_user_meta_data->>'full_name', ''),
        v_admin,
        v_admin,
        CASE WHEN v_admin THEN NOW() END
    )
    ON CONFLICT (id) DO NOTHING;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = public;
