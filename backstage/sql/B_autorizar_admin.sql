-- Manual alternative to the ADMIN_EMAILS setting of the database initialisation image (see docs/database-init.md):
-- approve a user and make them administrator. Replace the address, then run it in the SQL editor / psql.
UPDATE public.profiles
SET is_approved = TRUE, is_admin = TRUE, approved_at = NOW()
WHERE email = 'you@example.org';
