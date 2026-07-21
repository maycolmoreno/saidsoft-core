-- Se ejecuta una sola vez, al inicializar el volumen de datos de PostgreSQL.
-- Solo habilita la extensión; la conversión de muestra_metrica a hypertable la hace
-- una migración de Django (apps/monitoreo/migrations), porque la tabla debe existir antes.
CREATE EXTENSION IF NOT EXISTS timescaledb;
