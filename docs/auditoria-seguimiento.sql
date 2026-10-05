-- ============================================================================
-- SAIDSOFT - Seguimiento de la auditoria: cuantificar los hallazgos
--
-- Segunda vuelta de la bateria de docs/auditoria-integridad.sql. Esa encontro QUE
-- pasa; esta mide CUANTO. SOLO LECTURA: solo SELECT.
--
--   cd deploy
--   docker compose --env-file .env exec -T db --     sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' --     < ../docs/auditoria-seguimiento.sql > /tmp/seguimiento.txt 2>&1
--
-- Que mide cada bloque:
--   A, A2, A3  el doble escritor de muestra_red_farmacia, medido en filas/hora
--   B, C       parametros reales de escala, para rehacer la proyeccion a 1.300
--   D, D2      tamano en disco real (D2 suma los chunks, que D no ve)
--   E          si las 30 discrepancias de pos_bdd son un error de grupo
--   F          las 21 muestras de MAM06-A en 90 ms
--   G          si el cruce por numero de serie puede vincular mal
--   H, H2      censo de columnas (seccion 15 del informe)
--   I, J       crecimiento de evento_auditoria y churn de alertas
-- ============================================================================
\pset pager off

\echo '===== A. LA PRUEBA DEL DOBLE ESCRITOR: filas por farmacia en una hora ====='
-- Con UN sondeador cada 5 min deberian ser ~12 filas/hora por farmacia.
-- Con DOS, ~24. Esto lo mide directamente.
SELECT f.codigo AS farmacia,
       count(*) AS filas_ultima_hora,
       round(60.0 / NULLIF(count(*), 0), 2) AS minutos_entre_filas
FROM muestra_red_farmacia m
JOIN farmacia f ON f.id = m.farmacia_id
WHERE m.timestamp > now() - interval '1 hour'
GROUP BY f.codigo ORDER BY count(*) DESC LIMIT 15;

\echo '===== A2. Distribucion: cuantas farmacias tienen cuantas filas por hora ====='
SELECT filas_por_hora, count(*) AS cuantas_farmacias
FROM (SELECT farmacia_id, count(*) AS filas_por_hora
      FROM muestra_red_farmacia
      WHERE timestamp > now() - interval '1 hour'
      GROUP BY farmacia_id) t
GROUP BY filas_por_hora ORDER BY filas_por_hora;

\echo '===== A3. Huecos entre filas consecutivas de UNA farmacia (la mas activa) ====='
-- Si hay dos sondeadores, los intervalos alternan entre un valor chico y uno grande.
WITH top AS (
  SELECT farmacia_id FROM muestra_red_farmacia
  WHERE timestamp > now() - interval '2 hours'
  GROUP BY farmacia_id ORDER BY count(*) DESC LIMIT 1)
SELECT m.timestamp,
       round(EXTRACT(epoch FROM m.timestamp - lag(m.timestamp) OVER (ORDER BY m.timestamp))) AS seg_desde_anterior,
       m.red_recibido_kbps, m.red_enviado_kbps
FROM muestra_red_farmacia m
WHERE m.farmacia_id = (SELECT farmacia_id FROM top)
  AND m.timestamp > now() - interval '40 minutes'
ORDER BY m.timestamp;

\echo '===== B. PARAMETROS REALES DE ESCALA (para rehacer la proyeccion) ====='
SELECT (SELECT count(*) FROM farmacia)                                        AS farmacias,
       (SELECT count(*) FROM farmacia WHERE ip_router IS NOT NULL)            AS con_ip_router,
       (SELECT count(*) FROM estacion)                                        AS estaciones,
       (SELECT count(*) FROM estacion WHERE estado_aprobacion='aprobada')     AS aprobadas,
       (SELECT count(*) FROM estacion WHERE monitorear_recursos)              AS con_monitorear_recursos,
       (SELECT count(*) FROM estacion WHERE ultimo_heartbeat > now() - interval '1 day') AS latieron_hoy,
       (SELECT count(DISTINCT estacion_id) FROM muestra_servicio_pos
          WHERE timestamp > now() - interval '1 day')                         AS reportan_servicios_pos,
       (SELECT count(DISTINCT farmacia_id) FROM muestra_red_farmacia
          WHERE timestamp > now() - interval '1 day')                         AS farmacias_con_red;

\echo '===== C. TASA REAL DE INSERCION por tabla (ultimas 24 h) ====='
SELECT 'muestra_metrica' AS tabla, count(*) AS filas_24h,
       count(DISTINCT estacion_id) AS entidades,
       round(count(*)::numeric / NULLIF(count(DISTINCT estacion_id),0), 1) AS filas_por_entidad
FROM muestra_metrica WHERE timestamp > now() - interval '1 day'
UNION ALL
SELECT 'muestra_servicio_pos', count(*), count(DISTINCT estacion_id),
       round(count(*)::numeric / NULLIF(count(DISTINCT estacion_id),0), 1)
FROM muestra_servicio_pos WHERE timestamp > now() - interval '1 day'
UNION ALL
SELECT 'muestra_red_farmacia', count(*), count(DISTINCT farmacia_id),
       round(count(*)::numeric / NULLIF(count(DISTINCT farmacia_id),0), 1)
FROM muestra_red_farmacia WHERE timestamp > now() - interval '1 day'
UNION ALL
SELECT 'evento_monitoreo', count(*), count(DISTINCT estacion_id),
       round(count(*)::numeric / NULLIF(count(DISTINCT estacion_id),0), 1)
FROM evento_monitoreo WHERE timestamp > now() - interval '1 day';

\echo '===== D. TAMANO EN DISCO REAL de las 4 series ====='
SELECT c.relname AS tabla,
       pg_size_pretty(pg_total_relation_size(c.oid))      AS total,
       pg_size_pretty(pg_relation_size(c.oid))            AS solo_datos,
       pg_size_pretty(pg_indexes_size(c.oid))             AS solo_indices
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='public'
  AND c.relname IN ('muestra_metrica','muestra_servicio_pos','muestra_red_farmacia',
                    'evento_monitoreo','estacion','alerta','evento_auditoria')
ORDER BY pg_total_relation_size(c.oid) DESC;

\echo '===== D2. Tamano de los chunks de TimescaleDB (lo anterior no los suma) ====='
SELECT hypertable_name,
       count(*) AS chunks,
       pg_size_pretty(sum(total_bytes)) AS total,
       pg_size_pretty(sum(index_bytes)) AS indices
FROM timescaledb_information.hypertables h
JOIN LATERAL chunks_detailed_size(format('%I.%I', h.hypertable_schema, h.hypertable_name)) ON true
GROUP BY hypertable_name ORDER BY sum(total_bytes) DESC;

\echo '===== E. EL PATRON DE pos_bdd: agrupado, para ver si es un error de grupo ====='
SELECT g.codigo AS grupo_declarado,
       COALESCE(NULLIF(g.pos_bdd,''), lower(g.codigo)) AS bdd_que_declara_el_panel,
       e.pos_bdd AS bdd_que_reporta_el_pos,
       count(*) AS estaciones
FROM estacion e
JOIN farmacia f ON f.id = e.farmacia_id
JOIN grupo g ON g.id = f.grupo_id
WHERE e.pos_bdd <> ''
GROUP BY 1,2,3 ORDER BY 1,4 DESC;

\echo '===== F. Las 21 muestras de MAM06-A en 90 ms: que son ====='
SELECT timestamp, ram_total, ram_usada, cpu_carga_pct, disco_total_gb, latencia_ms
FROM muestra_metrica
WHERE estacion_id = (SELECT id FROM estacion WHERE codigo='MAM06-A')
ORDER BY timestamp LIMIT 25;

\echo '===== G. Riesgo del cruce por serial: hay Activos con serial de relleno? ====='
-- vincular_activos_por_numero_serie exige UN solo Activo candidato, pero NO exige una
-- sola Estacion. Si un Activo tuviera un serial de relleno, la primera estacion que se
-- procese se lo queda, arbitrariamente, y le sobrescribe la farmacia.
SELECT 'activos con serial de relleno' AS caso, count(*) AS cuantos,
       string_agg(codigo, ',') AS cuales
FROM activo
WHERE lower(trim(coalesce(numero_serie,''))) IN
      ('default string','system serial number','none','to be filled by o.e.m.',
       'o.e.m.','not specified','n/a','na','0','unknown','serial number')
UNION ALL
SELECT 'estaciones con serial de relleno', count(*), string_agg(codigo, ',')
FROM estacion
WHERE lower(trim(numero_serie)) IN
      ('default string','system serial number','none','to be filled by o.e.m.',
       'o.e.m.','not specified','n/a','na','0','unknown','serial number');

\echo '===== H. CENSO de columnas de farmacia (seccion 15 del informe) ====='
SELECT count(*) AS filas,
       count(*) FILTER (WHERE coordinador_zonal <> '')    AS coord_zonal,
       count(*) FILTER (WHERE coordinador_regional <> '') AS coord_regional,
       count(*) FILTER (WHERE parroquia <> '')            AS parroquia,
       count(extension_telefonica)                        AS ext_telef,
       count(fecha_inicio_ruc)                            AS f_ruc,
       count(fecha_inicio_operacion)                      AS f_oper,
       count(*) FILTER (WHERE tipo_sucursal <> '')        AS tipo_suc,
       count(tecnico_asignado_id)                         AS tecnico,
       count(ip_router)                                   AS ip_router,
       count(ancho_contratado_mbps)                       AS ancho,
       count(*) FILTER (WHERE circuito_proveedor <> '')    AS circuito,
       count(*) FILTER (WHERE administrador <> '')        AS administrador,
       count(*) FILTER (WHERE ciudad <> '')               AS ciudad,
       count(latitud)                                     AS latitud
FROM farmacia;

\echo '===== H2. CENSO de columnas de estacion ====='
SELECT count(*) AS estaciones,
       count(*) FILTER (WHERE hardware_id <> '')         AS con_hardware_id,
       count(*) FILTER (WHERE numero_serie <> '')        AS con_serie,
       count(*) FILTER (WHERE meshcentral_node_id <> '') AS con_meshcentral,
       count(bitlocker_habilitado)                       AS bitlocker,
       count(*) FILTER (WHERE power_plan_actual <> '')   AS power_plan,
       count(windows_update_pendientes)                  AS wu_escaneado,
       count(desfase_reloj_segundos)                     AS con_desfase,
       count(ram_total_mb)                               AS con_hardware_info,
       count(*) FILTER (WHERE pos_bdd <> '')             AS reporto_pos_bdd,
       count(*) FILTER (WHERE autocorrecciones_reloj > 0) AS se_autocorrigio,
       count(*) FILTER (WHERE pausado)                   AS pausadas
FROM estacion;

\echo '===== I. evento_auditoria: cuanto creció y desde cuando (no tiene retencion) ====='
SELECT count(*) AS filas, min(timestamp) AS desde, max(timestamp) AS hasta,
       pg_size_pretty(pg_total_relation_size('evento_auditoria')) AS tamano
FROM evento_auditoria;

\echo '===== J. Alertas: 586 resueltas con 5 abiertas, hay churn? ====='
SELECT r.metrica, a.estado, count(*) AS cuantas,
       round(avg(EXTRACT(epoch FROM a.resuelta_en - a.abierta_en))/60, 1) AS minutos_promedio_abierta
FROM alerta a JOIN regla_alerta r ON r.id = a.regla_id
GROUP BY r.metrica, a.estado ORDER BY count(*) DESC LIMIT 20;
