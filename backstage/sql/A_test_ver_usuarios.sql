-- Usuarios CONFIRMADOS
SELECT id, email, full_name, is_approved, approved_at
FROM public.profiles
WHERE is_approved = TRUE
ORDER BY approved_at DESC;

-- Usuarios PENDIENTES
SELECT id, email, full_name, is_approved, created_at
FROM public.profiles
WHERE is_approved = FALSE OR is_approved IS NULL
ORDER BY created_at DESC;

-- Resumen rápido
SELECT 
  COUNT(*) FILTER (WHERE is_approved = TRUE) AS confirmados,
  COUNT(*) FILTER (WHERE is_approved = FALSE OR is_approved IS NULL) AS pendientes,
  COUNT(*) AS total
FROM public.profiles;