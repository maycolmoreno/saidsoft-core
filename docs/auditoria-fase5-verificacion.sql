-- ============================================================================
-- SAIDSOFT - Verificacion de las correcciones de la Fase 5 (11-oct-2026)
--
-- Las CUATRO preguntas que las correcciones de `docs/auditoria/fase5-triage.md`
-- dejaron abiertas y que solo produccion puede responder. SOLO LECTURA: solo
-- SELECT, ni un UPDATE. Mismo molde que docs/auditoria-integridad.sql y
-- docs/auditoria-seguimiento.sql.
--
--   cd deploy
--   docker compose --env-file .env exec -T db \
--     sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
--     < ../docs/auditoria-fase5-verificacion.sql > /tmp/fase5.txt 2>&1
--
-- Van juntas a proposito: las cuatro necesitan la base de produccion, ninguna
-- cambia nada, y dos de ellas DECIDEN como se arregla un hallazgo que sigue
-- abierto. Correrlas de a una es cuatro pedidos de acceso en vez de uno.
--
--   A   INV-01  cuantos activos estaban congelados (ya corregido: mide el dano pasado)
--   B   INV-07  si hay numeros de serie duplicados  -> DECIDE el arreglo
--   C   B-2     cuantas estaciones no tienen identidad de hardware -> DECIDE el arreglo
--   D   L-1     cuanto cambia el KPI de flota con el denominador y el umbral nuevos
-- ============================================================================
\pset pager off

\echo '===== A. INV-01: activos que estaban congelados en el admin ====='
-- Cada fila era un equipo al que no se le podia corregir el modelo, la categoria,
-- el codigo SAP ni la garantia: el admin devolvia 500 (ValueError). Corregido el
-- 11-oct-2026 (§10-BJ), asi que esto NO es un pendiente: es cuanto dano habia.
--
-- Si devuelve 0 filas, el bug era real pero todavia no habia alcanzado a nadie
-- -- lo cual tambien es un dato, porque dice que se atrapo antes de que doliera.
SELECT a.id, a.codigo, e.codigo AS estacion, a.ip, a.mac, a.modelo
FROM activo a
JOIN estacion e ON e.id = a.estacion_id
WHERE a.estacion_id IS NOT NULL
  AND (a.ip IS NOT NULL OR a.mac <> '')
ORDER BY a.codigo;

\echo '===== B. INV-07: numeros de serie duplicados en el inventario ====='
-- `Activo.numero_serie` no tiene unicidad, y los tres cruces declarado<->RMM
-- (`vincular_activos_por_numero_serie`, `crear_activos_desde_estaciones`,
-- `datos_hardware_desde_estacion`) se apoyan en que sea unico. Con duplicados no
-- fallan: eligen uno, en silencio.
--
-- ESTO DECIDE EL ARREGLO. Sin filas, el arreglo es un constraint y listo. Con
-- filas, primero hay que resolverlas a mano -- una migracion que imponga unicidad
-- sobre datos sucios falla en el despliegue, que es la misma trampa que hizo
-- descartar el constraint de EventoEnlaceFarmacia en §10-BI.
SELECT a.numero_serie, COUNT(*) AS cuantos, string_agg(a.codigo, ', ' ORDER BY a.codigo) AS activos
FROM activo a
WHERE a.numero_serie <> ''
GROUP BY a.numero_serie
HAVING COUNT(*) > 1
ORDER BY COUNT(*) DESC, a.numero_serie;

\echo '===== B2. INV-07: series de relleno del BIOS, que NO son duplicados reales ====='
-- Contexto para leer B sin sobre-reaccionar: once equipos que dicen `default string`
-- no son once equipos con el mismo serial, son once sin serial grabado (ver
-- `SERIES_DE_RELLENO` en apps/activos/services.py). Si B se llena de estos, el
-- problema es de captura, no de unicidad.
SELECT lower(a.numero_serie) AS serie, COUNT(*) AS cuantos
FROM activo a
-- La lista es la de `SERIES_DE_RELLENO` (apps/activos/services.py), copiada entera y
-- no de memoria: si alla se agrega una, hay que agregarla aca.
WHERE lower(a.numero_serie) IN (
    'default string', 'system serial number', 'none',
    'to be filled by o.e.m.', 'to be filled by oem', 'not specified',
    'not applicable', 'n/a', 'unknown', 'invalid', 'system serialnumber',
    'base board serial number', 'chassis serial number', '0123456789'
)
GROUP BY lower(a.numero_serie)
ORDER BY COUNT(*) DESC;

\echo '===== C. B-2: estaciones sin identidad de hardware (trust-on-first-use) ====='
-- `hardware_id` es la unica defensa del servidor contra que un equipo ajeno pida el
-- token de una estacion y la suplante. Una estacion con el campo vacio acepta el
-- PRIMER valor que llegue, para siempre: son las creadas antes de que el mecanismo
-- existiera. `A-6` (§10-BM) quito el valor compartido y conocido del agente, pero
-- este hueco sigue abierto.
--
-- ESTO DECIDE EL ARREGLO. Si son pocas, se les fija la identidad a mano o se las
-- hace re-enrolar de forma controlada. Si son muchas, hace falta un camino en el
-- panel y no se puede endurecer la regla de golpe sin dejar estaciones afuera.
SELECT
    COUNT(*) FILTER (WHERE e.hardware_id = '')                        AS sin_identidad,
    COUNT(*) FILTER (WHERE e.hardware_id <> '')                       AS con_identidad,
    COUNT(*) FILTER (WHERE e.hardware_id = ''
                       AND e.estado_aprobacion = 'aprobada')          AS sin_identidad_y_aprobadas,
    COUNT(*)                                                           AS total
FROM estacion e;

\echo '--- C2. El detalle de las que no tienen identidad, para decidir que hacer ---'
SELECT e.codigo, f.codigo AS farmacia, e.estado_aprobacion, e.ultimo_heartbeat, e.version_agente
FROM estacion e
JOIN farmacia f ON f.id = e.farmacia_id
WHERE e.hardware_id = ''
ORDER BY e.estado_aprobacion, e.codigo;

\echo '===== D. L-1: cuanto cambia el KPI de flota ====='
-- El tablero cambio DOS cosas a la vez el 11-oct-2026 (§10-BI): el denominador
-- (antes salia de `calcular_matriz_cumplimiento`, que excluye el grupo PENDIENTE y
-- no filtraba por estado de aprobacion) y el umbral de "en linea" (5 -> 10 min).
--
-- Los numeros del panel VAN A CAMBIAR en el despliegue y eso es la correccion, no un
-- error. Conviene anotar los valores de antes el mismo dia y avisarle a la mesa de
-- ayuda. Esta consulta da las dos lecturas lado a lado.
SELECT
    COUNT(*) FILTER (WHERE e.estado_aprobacion = 'aprobada')                AS denominador_nuevo,
    COUNT(*) FILTER (WHERE e.estado_aprobacion = 'aprobada'
                       AND g.codigo <> 'PENDIENTE')                         AS denominador_viejo,
    COUNT(*) FILTER (WHERE e.estado_aprobacion = 'aprobada'
                       AND g.codigo = 'PENDIENTE')                          AS entran_por_el_cambio,
    COUNT(*) FILTER (WHERE e.estado_aprobacion = 'aprobada'
                       AND e.ultimo_heartbeat >= now() - interval '10 minutes') AS en_linea_umbral_nuevo,
    COUNT(*) FILTER (WHERE e.estado_aprobacion = 'aprobada'
                       AND e.ultimo_heartbeat >= now() - interval '5 minutes')  AS en_linea_umbral_viejo,
    COUNT(*) FILTER (WHERE e.estado_aprobacion = 'aprobada'
                       AND e.ultimo_heartbeat IS NULL)                      AS nunca_reportaron
FROM estacion e
JOIN farmacia f ON f.id = e.farmacia_id
JOIN grupo g ON g.id = f.grupo_id;

\echo '--- D2. Las farmacias aprobadas del grupo PENDIENTE, que son las que entran ---'
-- PENDIENTE es el bucket que usa `importar_farmacias` cuando no pudo resolver el
-- nodo de una fila. Que tenga estaciones aprobadas significa que falta terminar de
-- clasificarlas, y es una tarea aparte que esta consulta deja a la vista.
SELECT f.codigo AS farmacia, e.codigo AS estacion, e.ultimo_heartbeat
FROM estacion e
JOIN farmacia f ON f.id = e.farmacia_id
JOIN grupo g ON g.id = f.grupo_id
WHERE g.codigo = 'PENDIENTE'
  AND e.estado_aprobacion = 'aprobada'
ORDER BY f.codigo, e.codigo;

\echo '===== FIN. Ningun bloque escribio nada. ====='
