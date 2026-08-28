-- Runs once, on first initialisation of the pgdata volume.
-- The test suite needs its own database so it never touches the archive.
-- If you already have a pgdata volume, create it by hand instead:
--   docker compose exec postgres createdb -U bindery bindery_test
SELECT 'CREATE DATABASE bindery_test'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'bindery_test') \gexec
