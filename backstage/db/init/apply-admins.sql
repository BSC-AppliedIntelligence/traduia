-- Run by db-init.sh as postgres on every run. Variable: admin_emails (comma-separated, from ADMIN_EMAILS).
-- Replaces the list of bootstrap administrators and promotes users who already signed up (never demotes anyone).
BEGIN;
DELETE FROM public.bootstrap_admin_emails;
INSERT INTO public.bootstrap_admin_emails (email)
SELECT DISTINCT lower(btrim(x)) FROM unnest(string_to_array(:'admin_emails', ',')) AS x WHERE btrim(x) <> '';
UPDATE public.profiles
   SET is_approved = TRUE, is_admin = TRUE, approved_at = COALESCE(approved_at, NOW())
 WHERE lower(email) IN (SELECT email FROM public.bootstrap_admin_emails)
   AND (NOT is_approved OR NOT is_admin);
COMMIT;
