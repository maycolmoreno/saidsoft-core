-- ============================================================================
-- SAIDSOFT - Bateria de comprobaciones de integridad y duplicados logicos
--
-- QUE ES
--   28 comprobaciones de SOLO LECTURA sobre el esquema de SAIDSOFT. Buscan lo que
--   las constraints de la base hoy NO garantizan: duplicados logicos, FK que se
--   contradicen entre si, estado actual que no coincide con el ultimo evento,
--   series de tiempo huerfanas y cadenas vacias ocupando un cupo UNIQUE.
--   Salio de la auditoria de BD y codigo del 5-oct-2026.
--
-- GARANTIA
--   No modifica, no borra y no crea nada: solo SELECT y \echo. Se puede correr en
--   PRODUCCION en cualquier momento, incluso con el sistema en uso. Ninguna
--   consulta toma locks mas alla de los de lectura.
--
-- COMO SE CORRE (sin escribir ni imprimir ninguna credencial: psql corre DENTRO
-- del contenedor y toma usuario y base de sus propias variables de entorno)
--
--   cd deploy
--   docker compose --env-file .env exec -T db \
--     sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
--     < ../docs/auditoria-integridad.sql > /tmp/auditoria.txt 2>&1
--
--   Se invoca por el NOMBRE DE SERVICIO (db), no por el del contenedor: el
--   contenedor se llama <proyecto>-db-1 y el proyecto cambia segun desde donde se
--   haya levantado el stack, asi que "docker exec -i deploy-db-1" falla con
--   "No such container" en cualquier instalacion cuyo proyecto no sea "deploy".
--   Compose resuelve el servicio sin importar como quedo nombrado el contenedor.
--
-- COMO SE LEE
--   Cada bloque imprime su titulo y despues sus filas. "(0 rows)" = esa
--   comprobacion esta limpia. Cualquier fila devuelta es un caso a revisar: NO
--   son necesariamente errores, varias distinguen un hallazgo real de un dato
--   legitimo y hay que mirar el caso. El informe de la auditoria explica, para
--   cada numero, que significa encontrar filas ahi.
--
-- CUANDO CONVIENE REPETIRLA
--   Despues de cada carga masiva (importar_directorio_sucursales,
--   crear_activos_desde_rmm, importar_circuitos_proveedor), despues de un
--   re-enrolamiento de estaciones, y cada tanto como chequeo de rutina. Dos veces
--   en la historia de este proyecto algo que debia estar pasando no pasaba y nadie
--   se entero (las hypertables que nunca se crearon; ocho tareas diarias que nunca
--   disparaban): esa familia de fallas solo se ve mirando la base, no el codigo.
-- ============================================================================
\pset pager off
\timing off

\echo '===== 1. PUENTE User <-> Colaborador: los dos caminos en desacuerdo ====='
-- Colaborador.usuario (OneToOne) vs PerfilUsuario.colaborador. apps.viaticos.services
-- .colaborador_de() prueba los dos y prefiere el primero: si discrepan, el segundo se
-- ignora en silencio y un viatico puede quedar atribuido a otra persona.
SELECT pu.usuario_id,
       c1.id AS colaborador_por_usuario_onetoone,
       c1.nombre AS nombre_1,
       pu.colaborador_id AS colaborador_por_perfil,
       c2.nombre AS nombre_2
FROM perfil_usuario pu
JOIN colaborador c1 ON c1.usuario_id = pu.usuario_id
JOIN colaborador c2 ON c2.id = pu.colaborador_id
WHERE pu.colaborador_id IS DISTINCT FROM c1.id;

\echo '===== 2. Farmacia.tecnico_asignado sin login: cadena Colaborador->User rota ====='
-- Farmacia.tecnico_asignado es Colaborador, pero Mantenimiento/VisitaTecnica/
-- ActividadPlanificada.tecnico son auth.User. Sin Colaborador.usuario ni perfil, no se
-- puede responder "que mantenimientos hizo el tecnico asignado a esta farmacia".
SELECT f.codigo AS farmacia, c.id AS colaborador_id, c.nombre
FROM farmacia f
JOIN colaborador c ON c.id = f.tecnico_asignado_id
LEFT JOIN perfil_usuario pu ON pu.colaborador_id = c.id
WHERE c.usuario_id IS NULL AND pu.id IS NULL;

\echo '===== 3. Estacion vs Activo: farmacia contradictoria ====='
-- Activo.estacion y Activo.farmacia son dos FK independientes. Si el activo apunta a una
-- estacion de OTRA farmacia, las dos columnas se contradicen.
SELECT a.codigo AS activo, a.farmacia_id AS activo_farmacia,
       fa.codigo AS activo_farmacia_cod,
       e.codigo AS estacion, e.farmacia_id AS estacion_farmacia,
       fe.codigo AS estacion_farmacia_cod
FROM activo a
JOIN estacion e ON e.id = a.estacion_id
LEFT JOIN farmacia fa ON fa.id = a.farmacia_id
LEFT JOIN farmacia fe ON fe.id = e.farmacia_id
WHERE a.farmacia_id IS DISTINCT FROM e.farmacia_id;

\echo '===== 4. Activo.unidad_negocio vs Farmacia.unidad_negocio (fuga de tenant) ====='
SELECT a.codigo AS activo, a.unidad_negocio_id AS un_activo,
       f.codigo AS farmacia, f.unidad_negocio_id AS un_farmacia
FROM activo a
JOIN farmacia f ON f.id = a.farmacia_id
WHERE a.unidad_negocio_id IS NOT NULL
  AND a.unidad_negocio_id IS DISTINCT FROM f.unidad_negocio_id;

\echo '===== 5. Mantenimiento SIN cliente: queda sin tenant y lo ven TODOS los clientes ====='
-- `mantenimiento` no tiene unidad_negocio_id: el tenant se deriva de cliente->colaborador.
-- Las vistas usan scope_opcional_*, que incluye los NULL => visible para cualquier cliente.
SELECT count(*) AS mantenimientos_sin_cliente,
       (SELECT count(*) FROM mantenimiento) AS total,
       round(100.0 * count(*) / NULLIF((SELECT count(*) FROM mantenimiento), 0), 1) AS pct
FROM mantenimiento WHERE cliente_id IS NULL;

\echo '===== 6. Un mantenimiento con equipos de FARMACIAS DISTINTAS ====='
-- No hay una ruta unica al sitio del mantenimiento. Si sus equipos estan en farmacias
-- distintas, "donde se hizo" no tiene respuesta unica.
SELECT me.mantenimiento_id,
       count(DISTINCT a.farmacia_id) AS farmacias_distintas,
       string_agg(DISTINCT f.codigo, ',') AS cuales
FROM mantenimiento_equipo me
JOIN activo a ON a.id = me.equipo_id
LEFT JOIN farmacia f ON f.id = a.farmacia_id
WHERE a.farmacia_id IS NOT NULL
GROUP BY me.mantenimiento_id HAVING count(DISTINCT a.farmacia_id) > 1;

\echo '===== 6b. La visita y el equipo principal apuntan a farmacias distintas ====='
SELECT m.id AS mantenimiento, fv.codigo AS farmacia_de_la_visita, fa.codigo AS farmacia_del_equipo
FROM mantenimiento m
JOIN visita_tecnica v ON v.id = m.visita_id
LEFT JOIN farmacia fv ON fv.id = v.farmacia_id
JOIN mantenimiento_equipo me ON me.mantenimiento_id = m.id AND me.es_principal
JOIN activo a ON a.id = me.equipo_id
LEFT JOIN farmacia fa ON fa.id = a.farmacia_id
WHERE a.farmacia_id IS DISTINCT FROM v.farmacia_id;

\echo '===== 7. EstadoRedActivo: la estacion que sondeo no es de la farmacia del activo ====='
SELECT era.id, a.codigo AS activo, fa.codigo AS farmacia_activo,
       e.codigo AS estacion_sondeo, fe.codigo AS farmacia_estacion
FROM estado_red_activo era
JOIN activo a ON a.id = era.activo_id
JOIN estacion e ON e.id = era.estacion_que_sondeo_id
LEFT JOIN farmacia fa ON fa.id = a.farmacia_id
LEFT JOIN farmacia fe ON fe.id = e.farmacia_id
WHERE a.farmacia_id IS DISTINCT FROM e.farmacia_id;

\echo '===== 8. ReporteViatico: farmacia visitada fuera de la zona del colaborador ====='
SELECT rv.id, c.nombre AS colaborador, f.codigo AS farmacia_visitada, cz.zona_cobertura
FROM reporte_viatico rv
JOIN colaborador c ON c.id = rv.colaborador_id
JOIN farmacia f ON f.id = rv.farmacia_visitada_id
JOIN colaborador_zona cz ON cz.colaborador_id = c.id AND cz.activa
WHERE NOT EXISTS (
    SELECT 1 FROM colaborador_zona_farmacias_asignadas x
    WHERE x.colaboradorzona_id = cz.id AND x.farmacia_id = f.id
);

\echo '===== 9. Estado actual vs ultimo evento: EstadoDispositivo contra EventoMonitoreo ====='
-- EventoMonitoreo solo se escribe en las TRANSICIONES. El ultimo evento de cada
-- (estacion,fuente) deberia coincidir con el en_linea del EstadoDispositivo.
WITH ultimo AS (
    SELECT DISTINCT ON (estacion_id, fuente) estacion_id, fuente, en_linea, timestamp
    FROM evento_monitoreo ORDER BY estacion_id, fuente, timestamp DESC
)
SELECT e.codigo AS estacion, ed.fuente,
       ed.en_linea AS estado_actual, u.en_linea AS ultimo_evento, u.timestamp
FROM estado_dispositivo ed
JOIN estacion e ON e.id = ed.estacion_id
JOIN ultimo u ON u.estacion_id = ed.estacion_id AND u.fuente = ed.fuente
WHERE ed.en_linea IS DISTINCT FROM u.en_linea;

\echo '===== 10. Estacion.estado_conexion vs EstadoDispositivo(mqtt) ====='
SELECT e.codigo, e.estado_conexion, ed.en_linea AS mqtt_en_linea,
       e.ultimo_heartbeat, ed.actualizado_en
FROM estacion e
JOIN estado_dispositivo ed ON ed.estacion_id = e.id AND ed.fuente = 'mqtt'
WHERE (e.estado_conexion = 'online') IS DISTINCT FROM ed.en_linea;

\echo '===== 11. Estacion.pos_bdd (lo que reporta el POS) vs Grupo (lo que dice el panel) ====='
-- Estacion.nodo_discrepante en SQL. Es informativo por diseno, pero cuantifica el desfase
-- entre lo declarado en el panel y lo que el equipo realmente corre.
SELECT e.codigo AS estacion, e.pos_bdd AS reporta_el_pos,
       COALESCE(NULLIF(g.pos_bdd,''), lower(g.codigo)) AS deberia_ser, g.codigo AS grupo
FROM estacion e
JOIN farmacia f ON f.id = e.farmacia_id
JOIN grupo g ON g.id = f.grupo_id
WHERE e.pos_bdd <> ''
  AND lower(trim(e.pos_bdd)) <> lower(trim(COALESCE(NULLIF(g.pos_bdd,''), lower(g.codigo))));

\echo '===== 12. EquipoBordeFarmacia: el router dice llamarse distinto de la farmacia ====='
-- nombre_coincide en SQL: si ip_router de una farmacia apunta a otro equipo, TODO lo que
-- se monitorea de esa farmacia es de otra.
SELECT f.codigo AS farmacia, eb.nombre_sistema AS dice_llamarse, eb.modelo, eb.ultima_lectura
FROM equipo_borde_farmacia eb
JOIN farmacia f ON f.id = eb.farmacia_id
WHERE eb.nombre_sistema <> '' AND upper(trim(eb.nombre_sistema)) <> upper(trim(f.codigo));

\echo '===== 13. Metricas de estaciones que NO tienen monitorear_recursos ====='
-- Si hay filas, se esta guardando una serie que el panel no espera de esa estacion.
SELECT e.codigo, e.monitorear_recursos, count(*) AS muestras,
       min(mm.timestamp) AS desde, max(mm.timestamp) AS hasta
FROM muestra_metrica mm JOIN estacion e ON e.id = mm.estacion_id
WHERE NOT e.monitorear_recursos
GROUP BY e.codigo, e.monitorear_recursos ORDER BY muestras DESC LIMIT 20;

\echo '===== 14. Metricas/eventos de estaciones NO aprobadas ====='
SELECT e.codigo, e.estado_aprobacion, count(*) AS muestras
FROM muestra_metrica mm JOIN estacion e ON e.id = mm.estacion_id
WHERE e.estado_aprobacion <> 'aprobada'
GROUP BY e.codigo, e.estado_aprobacion ORDER BY muestras DESC LIMIT 20;

\echo '===== 15. Alerta abierta de una estacion que ya no reporta problema / huerfanas ====='
SELECT a.estado, count(*) AS alertas,
       count(*) FILTER (WHERE a.estacion_id IS NULL) AS sin_estacion,
       count(*) FILTER (WHERE a.regla_id IS NULL) AS sin_regla
FROM alerta a GROUP BY a.estado ORDER BY alertas DESC;

\echo '===== 16. Duplicados logicos: EventoDespliegue repetido por (resultado,paso) ====='
SELECT count(*) AS filas_totales,
       count(DISTINCT (resultado_id, paso)) AS pares_distintos,
       count(*) - count(DISTINCT (resultado_id, paso)) AS filas_excedentes
FROM evento_despliegue;

\echo '===== 17. Idem para EventoInstalacion ====='
SELECT count(*) AS filas_totales,
       count(DISTINCT (resultado_id, paso)) AS pares_distintos,
       count(*) - count(DISTINCT (resultado_id, paso)) AS filas_excedentes
FROM evento_instalacion;

\echo '===== 18. Duplicados logicos en catalogos: mismo nombre normalizado ====='
SELECT 'marca' AS tabla, lower(trim(nombre)) AS valor, count(*) AS veces,
       string_agg(id::text, ',' ORDER BY id) AS ids
FROM marca GROUP BY 2 HAVING count(*) > 1
UNION ALL
SELECT 'categoria_equipo', lower(trim(nombre)), count(*), string_agg(id::text, ',' ORDER BY id)
FROM categoria_equipo GROUP BY 2 HAVING count(*) > 1
UNION ALL
SELECT 'departamento', lower(trim(nombre)), count(*), string_agg(id::text, ',' ORDER BY id)
FROM departamento GROUP BY 2 HAVING count(*) > 1
UNION ALL
SELECT 'tipo_mantenimiento', lower(trim(nombre)), count(*), string_agg(id::text, ',' ORDER BY id)
FROM tipo_mantenimiento GROUP BY 2 HAVING count(*) > 1
UNION ALL
SELECT 'tipo_consumible', lower(trim(nombre)), count(*), string_agg(id::text, ',' ORDER BY id)
FROM tipo_consumible GROUP BY 2 HAVING count(*) > 1
UNION ALL
SELECT 'ubicacion', lower(trim(nombre)), count(*), string_agg(id::text, ',' ORDER BY id)
FROM ubicacion GROUP BY 2 HAVING count(*) > 1
UNION ALL
SELECT 'farmacia.nombre', lower(trim(nombre)), count(*), string_agg(id::text, ',' ORDER BY id)
FROM farmacia WHERE nombre <> '' GROUP BY 2 HAVING count(*) > 1
UNION ALL
SELECT 'colaborador.nombre', lower(trim(nombre)), count(*), string_agg(id::text, ',' ORDER BY id)
FROM colaborador GROUP BY 2 HAVING count(*) > 1
UNION ALL
SELECT 'colaborador.correo', lower(trim(correo)), count(*), string_agg(id::text, ',' ORDER BY id)
FROM colaborador WHERE correo <> '' GROUP BY 2 HAVING count(*) > 1;

\echo '===== 19. Cadenas vacias ocupando el unico cupo UNIQUE ====='
SELECT 'categoria_equipo.codigo' AS columna, count(*) AS filas_vacias FROM categoria_equipo WHERE codigo = ''
UNION ALL SELECT 'tipo_consumible.codigo', count(*) FROM tipo_consumible WHERE codigo = ''
UNION ALL SELECT 'tipo_mantenimiento.codigo', count(*) FROM tipo_mantenimiento WHERE codigo = ''
UNION ALL SELECT 'bodega.codigo', count(*) FROM bodega WHERE codigo = ''
UNION ALL SELECT 'colaborador.cedula', count(*) FROM colaborador WHERE cedula = ''
UNION ALL SELECT 'marca.nombre', count(*) FROM marca WHERE nombre = ''
UNION ALL SELECT 'departamento.nombre', count(*) FROM departamento WHERE nombre = ''
UNION ALL SELECT 'orden_compra.numero_oc', count(*) FROM orden_compra WHERE numero_oc = ''
UNION ALL SELECT 'unidad_negocio.codigo', count(*) FROM unidad_negocio WHERE codigo = ''
UNION ALL SELECT 'grupo.codigo', count(*) FROM grupo WHERE codigo = '';

\echo '===== 20. MAC duplicada entre Activo declarado y DispositivoDetectado ====='
-- La MAC es la identidad del dispositivo. Activo.mac NO lleva unique (a proposito), asi
-- que el mismo equipo puede estar declarado dos veces en la misma farmacia.
SELECT farmacia_id, lower(mac) AS mac, count(*) AS veces, string_agg(codigo, ',') AS activos
FROM activo WHERE mac IS NOT NULL AND mac <> ''
GROUP BY farmacia_id, lower(mac) HAVING count(*) > 1;

\echo '===== 21. IP duplicada dentro de la misma farmacia (activos declarados) ====='
SELECT farmacia_id, ip, count(*) AS veces, string_agg(codigo, ',') AS activos
FROM activo WHERE ip IS NOT NULL
GROUP BY farmacia_id, ip HAVING count(*) > 1;

\echo '===== 22. Numero de serie repetido (deberia identificar un equipo fisico) ====='
SELECT 'activo.numero_serie' AS fuente, lower(trim(numero_serie)) AS serie, count(*) AS veces,
       string_agg(codigo, ',') AS donde
FROM activo WHERE numero_serie IS NOT NULL AND trim(numero_serie) <> ''
GROUP BY 2 HAVING count(*) > 1
UNION ALL
SELECT 'estacion.numero_serie', lower(trim(numero_serie)), count(*), string_agg(codigo, ',')
FROM estacion WHERE trim(numero_serie) <> ''
GROUP BY 2 HAVING count(*) > 1;

\echo '===== 23. hardware_id repetido entre estaciones (deberia ser unico por equipo) ====='
-- Se exige en el re-enrolamiento para evitar suplantacion, pero no lleva UNIQUE.
SELECT hardware_id, count(*) AS veces, string_agg(codigo, ',') AS estaciones
FROM estacion WHERE hardware_id <> ''
GROUP BY hardware_id HAVING count(*) > 1;

\echo '===== 24. Estacion cuyo codigo no corresponde a su farmacia (FARMACIA-SUFIJO) ====='
SELECT e.codigo AS estacion, f.codigo AS farmacia
FROM estacion e JOIN farmacia f ON f.id = e.farmacia_id
WHERE split_part(e.codigo, '-', 1) <> f.codigo;

\echo '===== 25. Huerfanos: series de tiempo sin entidad viva (deberia ser 0 por FK) ====='
SELECT 'muestra_metrica sin estacion' AS caso, count(*) FROM muestra_metrica mm
  LEFT JOIN estacion e ON e.id = mm.estacion_id WHERE e.id IS NULL
UNION ALL SELECT 'evento_monitoreo sin estacion', count(*) FROM evento_monitoreo em
  LEFT JOIN estacion e ON e.id = em.estacion_id WHERE e.id IS NULL
UNION ALL SELECT 'muestra_servicio_pos sin estacion', count(*) FROM muestra_servicio_pos ms
  LEFT JOIN estacion e ON e.id = ms.estacion_id WHERE e.id IS NULL
UNION ALL SELECT 'muestra_red_farmacia sin farmacia', count(*) FROM muestra_red_farmacia mr
  LEFT JOIN farmacia f ON f.id = mr.farmacia_id WHERE f.id IS NULL;

\echo '===== 26. EstadoServicioPos con clave fuera del catalogo activo ====='
SELECT esp.servicio, count(*) AS filas,
       bool_or(spm.id IS NOT NULL) AS existe_en_catalogo,
       bool_or(COALESCE(spm.activo,false)) AS alguno_activo
FROM estado_servicio_pos esp
LEFT JOIN servicio_pos_monitoreado spm ON spm.clave = esp.servicio
GROUP BY esp.servicio ORDER BY filas DESC;

\echo '===== 27. Volumen y ventana real de las series de tiempo ====='
SELECT 'muestra_metrica' AS tabla, count(*) AS filas, min(timestamp) AS desde, max(timestamp) AS hasta
FROM muestra_metrica
UNION ALL SELECT 'muestra_servicio_pos', count(*), min(timestamp), max(timestamp) FROM muestra_servicio_pos
UNION ALL SELECT 'muestra_red_farmacia', count(*), min(timestamp), max(timestamp) FROM muestra_red_farmacia
UNION ALL SELECT 'evento_monitoreo', count(*), min(timestamp), max(timestamp) FROM evento_monitoreo;
