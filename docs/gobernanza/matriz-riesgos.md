# Matriz de riesgos — SAIDSOFT

Inventario de riesgos identificados en la auditoría de gobernanza del 22-ago-2026
(ver `PLAN_MODERNIZACION.md`, sección "Auditoría de pendientes") y su estado actual.
Escala de probabilidad e impacto: **Baja / Media / Alta**. Nivel de riesgo = combinación
cualitativa de ambas. Un riesgo "Cerrado" significa que el tratamiento ya se implementó
y se verificó (con tests y, cuando aplicaba, contra el servidor real) — no que el
riesgo residual sea cero; ver la columna "Riesgo residual" cuando corresponda.

Esta matriz es un documento vivo: se actualiza cada vez que se cierra, se descubre o se
reprioriza un riesgo. La fuente de verdad detallada de cada fix es
`PLAN_MODERNIZACION.md`; acá se resume para gestión.

**Última revisión: 27-oct-2026.** Incorpora la auditoría técnica del 26-oct-2026
(`docs/DOCUMENTACION_TECNICA.md`, Parte I §2 y Parte II §14; los IDs `S-xx` remiten a
sus hallazgos de seguridad) y las correcciones de la rama `seguridad/pre-auditoria`.
"Corregido en código" = cambio hecho y probado en el repositorio, **pendiente de
desplegar** en el servidor; pasa a "Cerrado" cuando se verifica en producción.

## Riesgos cerrados

| ID | Activo / Proceso | Amenaza o vulnerabilidad | Prob. | Impacto | Nivel | Tratamiento | Estado | Ref. |
|---|---|---|---|---|---|---|---|---|
| R-01 | Backups | El cron de respaldo nunca corría — cero backups reales existían | Alta | Alta | **Crítico** | Respaldo diario instalado y verificado con corrida real; desde el 7-sep-2026 por `saidsoft-respaldo.timer` (systemd, recupera la corrida perdida tras un apagado) | Cerrado 22-ago-2026 | OPS-1 |
| R-02 | Panel web | Servido por HTTP plano, credenciales y sesión viajaban sin cifrar | Media | Alta | **Alto** | HTTPS obligatorio, cookies `Secure`, excepción documentada para `/media/` (agentes sin CA configurada) | Cerrado 22-ago-2026 | SEC-2 |
| R-03 | Vistas del panel | ~85% de las vistas solo pedían sesión iniciada, sin permiso por rol | Alta | Alta | **Crítico** | `@permission_required` por vista, permisos estándar de Django, roles en `seed_permisos.py` | Cerrado 22-ago-2026 (con corrección posterior, ver R-03b) | AC-1 |
| R-03b | Vistas del panel (`scripts.py`/`estaciones.py`/`monitoreo.py`) | El cierre de AC-1 se saltó 15 vistas de tres módulos sin revisarlas | Media | Alta | **Alto** | Barrido completo del panel confirmando cero vistas sin permiso restantes | Cerrado 22-ago-2026 | AC-1 (corrección) |
| R-04 | Aprobación de despliegues | La regla de "cuatro ojos" no exigía ningún permiso — cualquier segundo usuario contaba | Media | Alta | **Alto** | Permiso `aprobar_despliegue` explícito, no otorgado a ningún rol operativo por defecto | Cerrado 22-ago-2026 | AC-2 |
| R-05 | Ejecución de scripts a destino amplio | Sin aprobación de un segundo usuario para correr código en toda la cadena | Media | Alta | **Alto** | Mismo esquema de cuatro ojos que despliegues (`aprobar_ejecucionscript`) | Cerrado 22-ago-2026 | AC-3 |
| R-06 | Login del panel | Fuerza bruta ilimitada contra `/login/` y `/admin/` | Alta | Alta | **Crítico** | `django-axes`, bloqueo por usuario tras 5 intentos | Cerrado 22-ago-2026 | SEC-3 |
| R-07 | Stock de bodega | Condición de carrera: dos salidas/entregas simultáneas podían perder una actualización | Baja | Media | Medio | `select_for_update` + `F()` en vez de leer-modificar-escribir en Python | Cerrado 22-ago-2026 | BUG-1 |
| R-08 | Ciclo de vida de `Activo` | Transiciones sin `transaction.atomic` — un fallo a mitad de camino dejaba estado sin historial | Baja | Media | Medio | Las 9 funciones de transición envueltas en `@transaction.atomic` | Cerrado 22-ago-2026 | BUG-2 |
| R-09 | Firma de comandos MQTT | La firma de un comando sin parámetros era un string constante — reproducible indefinidamente | Media | Alta | **Alto** | `estación`+`timestamp` entran a la firma; ventana de 120s | Cerrado | SEC-1 |
| R-09b | Mensajes de Despliegue/SolicitudInstalacion | No tenían firma alguna (peor que R-09) — solo la ACL del broker los protegía | Media | **Crítica** | **Crítico** | Mismo esquema de firma que R-09 | Cerrado (código), desplegado | SEC-1 |
| R-11 | Herramienta de desarrollo `simular_agente.py` | No respetaba `MQTT_USE_TLS` — se conectaba siempre en plano | Baja (solo dev) | Baja | Bajo | Mismo chequeo de TLS que el resto del código | Cerrado 22-ago-2026 | SEC-6 |
| R-12 | Sesiones del panel | Sin expiración configurada — el default de Django (2 semanas, sobrevive cierre del navegador) | Media | Media | Medio | 8 horas de inactividad + cierre al cerrar el navegador | Cerrado 22-ago-2026 | SEC-5 |
| R-13 | Autenticación del panel | Sin segundo factor disponible en ninguna cuenta | Media | Alta | **Alto** | MFA (TOTP) disponible, opcional por usuario | Cerrado parcialmente — ver R-13b | SEC-4 |
| R-14 | Backups | Quedaban sin cifrar en el propio servidor | Media | Alta | **Alto** | Cifrado GPG/AES256, script se niega a correr sin la passphrase | Cerrado 22-ago-2026 | OPS-2 |
| R-15 | Backups | Nunca se había probado restaurar uno real (solo integridad del archivo) | Media | **Crítica** | **Crítico** | Restauración real ejecutada y verificada; repetida el 21-oct-2026 en `restore_test` con resultado idéntico | Cerrado 22-ago-2026 | OPS-2 |
| R-16 | `Activo`/`MovimientoInventario`/`RecepcionLote` | Estados declarados (`EN_TRANSITO`, `AJUSTE`, `ANULADO`) sin funcionalidad real detrás | Baja | Baja | Bajo | `AJUSTE`/`ANULADO` implementados; `EN_TRANSITO` eliminado (no correspondía al dominio) | Cerrado 22-ago-2026 | BUG-3 |
| R-19 | Credencial MQTT compartida (`MQTT_USERNAME_AGENTE`) | Tenía ACL `/saidsof/#` (acceso a todo) | Baja | Media | Medio | ACL angostada a solo enrolamiento con `deploy/emqx-narrow-acl-agente.sh` (12-oct-2026; 46 estaciones siguieron latiendo, cero clientes con el usuario compartido) | Cerrado 12-oct-2026. Ver R-25 (el bootstrap la reabría) | SEC-1 |
| R-22 | Gobernanza documental | Ausencia de política formal de seguridad, matriz de riesgos y SoA hasta esta fecha | — | — | — | Este mismo conjunto de documentos | Cerrado 22-ago-2026 | GOV-1 |
| R-26 | Imágenes Docker | Tres copias del `.env` de producción (117 variables) dentro de cada imagen (13-oct-2026) | Media | Alta | **Alto** | `.dockerignore` por prefijo (`.env.*`); verificado el 26-oct-2026 en el servidor: solo quedan las imágenes del último despliegue, ninguna vieja | Cerrado 26-oct-2026. Rotación de lo expuesto: ver R-23 | S-01 |
| R-27 | Panel en 502 tras corte de luz | `staticfiles.json` truncado dejaba `web` en bucle de reinicio (25-oct-2026, 7,5 h) | Media | Alta | **Alto** | `deploy/entrypoint.sh` descarta un manifiesto ilegible antes de `collectstatic` (commit `9c9405b`) | Cerrado (código desplegable) | — |

## Riesgos abiertos

| ID | Activo / Proceso | Amenaza o vulnerabilidad | Prob. | Impacto | Nivel | Tratamiento | Estado / plazo | Ref. |
|---|---|---|---|---|---|---|---|---|
| R-13b | Cuentas del panel | MFA existe pero no es obligatorio; al 22-oct-2026, **0 de 12 cuentas activas** lo tienen, incluidos 2 superusuarios (uno llamado `prueba`) y una cuenta posiblemente compartida | Media | Alta | **Alto** | 1) Desactivar cuentas de prueba y compartidas — **cuenta `prueba` desactivada (27-oct-2026)**. 2) MFA en superusuarios. 3) `MFA_OBLIGATORIO=True` con `MFA_PLAZO_ENROLAMIENTO`. Verificación: `manage.py estado_mfa` | Parcial; pasos 2 y 3 antes de la auditoría externa (semana del 2-nov-2026) | SEC-4, S-02 |
| R-17 | Backups | Copia en un disco USB conectado al NUC (27-oct-2026): cubre la falla del disco interno, pero no robo, incendio, sobretensión ni ransomware, porque está en el mismo equipo y lugar. `.env`, `certs` y passphrase guardados fuera (KeePass + USB, 20-oct-2026) | Baja | **Crítica** | Medio | 1) Que `backup.sh` copie solo al disco USB: `BACKUP_OFFSITE_DESTINO=<punto de montaje>` (acepta ruta local). 2) Copia realmente fuera del sitio: rotar dos discos (uno siempre fuera) o `BACKUP_ONEDRIVE_REMOTO`. 3) Probar una restauración desde esa copia | Parcial | OPS-2, S-03 |
| R-20 | Servidor físico (Intel NUC) | Cortes de luz del 24–25-oct-2026 dejaron el servidor ciego tres veces | Baja | Alta | Medio | **UPS instalada y encendido automático tras corte activado en BIOS (27-oct-2026).** Falta: apagado ordenado por USB si la UPS lo permite. Seguridad física del sitio sin evaluar | Mitigado | GOV-1 |
| R-21 | Disponibilidad del stack | Un único servidor físico sin sitio de respaldo; ~92 estaciones con agente y ~700 farmacias vigiladas | Baja | Alta | Medio | Aceptado para la escala actual. Mitigación: UPS (R-20), aviso externo (R-30), respaldo externo (R-17); `deploy/MIGRACION_SERVIDOR.md` documenta el reemplazo | Aceptado | GOV-1 |
| R-23 | Secretos de la plataforma | Secretos expuestos en el pasado sin rotación registrada: zip del instalador público con `COMANDO_HMAC_SECRET` y `MQTT_PASSWORD_AGENTE` (4-sep-2026) y copias del `.env` en imágenes (13-oct-2026, ver R-26) | Media | Alta | **Alto** | Rotar primero lo que no afecta a la flota (`SECRET_KEY`, base, Telegram, MeshCentral, dashboard EMQX); los dos de la flota cuando todas las estaciones tengan secreto propio. Registrar fecha de cada rotación | Rotación de la primera tanda antes de la auditoría | S-01 |
| R-24 | Enrolamiento de estaciones (P0-2) | Con la credencial compartida se puede leer la respuesta de enrolamiento de cualquier estación (token, `hmac_secret`, contraseña MQTT); el agente acepta respuestas no solicitadas | Baja | Alta | **Alto** | Nonce por solicitud y cifrado de la respuesta, o credencial de arranque por instalación (enlace de un solo uso ya existe) | Diseño pendiente; requiere versión nueva del agente | S-04 |
| R-25 | `deploy/bootstrap-emqx.sh` | Re-correrlo para recuperar EMQX reabría `/saidsof/#` a la credencial compartida (deshacía R-19) | Media | Alta | **Alto** | El script siembra la ACL angosta (27-oct-2026) | **Corregido en código** | S-05 |
| R-28 | Catálogos firmados (P0-1) | Agentes anteriores a 0.38 aceptan catálogos con la firma HMAC compartida (que estuvo expuesta, R-23) | Baja | Alta | **Alto** | Rollout del agente 0.38 (firma Ed25519) por tandas con canario; medir con `manage.py estado_catalogos` | Plan de rollout pendiente | S-06 |
| R-29 | Red del servidor | Puertos 8080, 8081, 8083, 8084 y 8085/udp en todas las interfaces, sin filtrado en el host (`DOCKER-USER` vacía, `ufw` inactivo); el servidor sale por el WiFi de **invitados** | Media | Alta | **Alto** | Pasar a la red cableada (segmento 10.111.1.x, pedido a redes del 27-oct-2026) y filtrar por origen en el host o en el firewall perimetral | Depende de redes | S-07 |
| R-30 | Monitoreo del propio servidor | La verificación de salud y el respaldo solo escribían en el journal: el 502 del 25-oct-2026 duró 7,5 h sin aviso | Alta | Alta | **Alto** | `OnFailure=` → `saidsoft-aviso@.service` manda la falla por Telegram desde el host (27-oct-2026) | **Corregido en código**; instalar la unidad en el servidor | S-14 |
| R-31 | Respaldo diario | `backup.sh` usaba `docker-compose` v1 cuando el host pasó a v2 (4-sep-2026); la restauración no cortaba ante un error | Media | Alta | **Alto** | Scripts pasados a `docker compose` v2; restauración con `ON_ERROR_STOP` y transacción única (27-oct-2026) | **Corregido en código**; verificar `journalctl -u saidsoft-respaldo` | S-21 |
| R-32 | Facturas de viáticos | Se servían sin sesión en `/media/viaticos/` | Baja | Media | Medio | Entrega por vista con permiso y alcance por cliente; ruta `internal` en nginx (27-oct-2026). Módulo diferido en 1.0 | **Corregido en código** | S-08 |
| R-33 | API Key de EMQX | Sin vencimiento y por encima de las ACL | Baja | Alta | Medio | Fecha de vencimiento y rotación anual; solo la reciben `web` y `worker` (R-8, 20-oct-2026) | Pendiente | S-09 |
| R-34 | Certificado del panel | Autofirmado: los usuarios aceptan advertencias del navegador | Baja | Media | Medio | Instalar `cert.pem` como confiable en los equipos de operadores o CA interna | Pendiente | S-10 |
| R-35 | Dependencias | Versiones con `>=`, sin archivo de bloqueo ni escaneo de vulnerabilidades; MeshCentral con imagen `latest` | Media | Media | Medio | Lockfile con hashes, `pip-audit` en CI, fijar imágenes | Pendiente | S-11 |
| R-36 | Host Docker | Todos los secretos llegan a los 7 contenedores de la app; el usuario `glpi` está en el grupo `docker` (equivale a root) | Baja | Alta | Medio | Permisos `600` en `deploy/.env` y `certs/key.pem`; lista de quién accede por SSH | Parcial | S-12 |
| R-37 | Bitácora de auditoría | `EventoAuditoria` solo impide el borrado por instancia; cambios de usuarios y permisos por `/admin/` no pasan por `registrar_evento` | Baja | Media | Medio | Revocar `DELETE/UPDATE` sobre la tabla al usuario de la app; auditar cambios de `User`/grupos | Pendiente | S-13 |
| R-38 | SNMP de los routers | La community es el código de la farmacia en minúscula (predecible) | Media | Media | Medio | SNMPv3 o communities aleatorias; confirmar solo lectura y filtro por IP de origen | Pendiente | S-15 |
| R-39 | Logs | El token FCM de los teléfonos se escribía en los logs | Baja | Baja | Bajo | Ya no se registra (27-oct-2026) | **Corregido en código** | S-16 |
| R-40 | Servicios externos | Contexto de alertas a Anthropic (opcional), Telegram, Meta (apagado) y OneDrive (sin configurar) | — | — | Informativo | Declararlo en la SoA (5.23) y en la política | Actualizado en la SoA | S-22 |
| R-18 | Firma de comandos MQTT | Secreto HMAC compartido para estaciones anteriores a 0.21 | Baja | Media | Medio | Desde 0.21 cada estación tiene secreto propio; queda para las que no actualizaron | Aceptado; se cierra con el rollout de R-28 | SEC-1 |

## Cómo se mantiene esta matriz

- Cada vez que `PLAN_MODERNIZACION.md` registra el cierre de un hallazgo de seguridad,
  gobernanza u operaciones, se refleja acá con su ID y referencia cruzada.
- Un riesgo "cerrado" que reabre (por una regresión, o porque cambian las
  circunstancias — ej. la flota de estaciones crece) vuelve a la tabla de abiertos con
  una nota explicando por qué se reabrió.
- Un riesgo "Corregido en código" pasa a "Cerrado" solo cuando se verifica en el
  servidor, con la fecha de esa verificación.
- Revisión mínima: junto con cada revisión anual de la política de seguridad
  (`politica-seguridad-informacion.md`), o antes si se cierra/descubre un riesgo con
  nivel **Alto** o **Crítico**.
