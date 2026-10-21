# Auditoría de seguridad y estabilidad

**Fecha:** 20-oct-2026 · **Commit:** `68d0045` · **Modo:** solo lectura. No se modificó nada y no se
ejecutó ninguna prueba de intrusión.

**Qué consolida:**
- `docs/auditoria/verificacion-seguridad-p0.md` (P0-1, P0-2; producción el 19-oct);
- `docs/auditoria/plan_remediacion_seguridad_continuidad.md` (R-1 a R-8);
- `docs/auditoria/gestion-remota-software.md` (C-1 a C-13);
- `docs/capacidad-2030.md`.

Esos documentos tienen el detalle técnico. Acá está el **estado al 20-oct**, con lo que se cerró
desde entonces y lo nuevo que encontró esta revisión.

**Cómo leer el estado:**
- **Confirmado en producción:** medido en el servidor real, con fecha.
- **Confirmado en código:** se lee en el código, pero no se ejecutó.
- **NO VERIFICADO:** falta evidencia.

Ningún hallazgo se declara explotado.

---

## 1. Hallazgos abiertos

Criticidad: Crítica / Alta / Media / Baja. Impacto y probabilidad: Alto / Medio / Bajo.

| ID | Hallazgo | Criticidad | Impacto | Prob. | Estado y evidencia |
|---|---|---|---|---|---|
| S-01 | **P0-1: contenido del catálogo de eventos interpretado por PowerShell en el agente, y agentes que aceptan catálogos sin firma.** La validación del servidor (`01f982b`) cubre lo que publica SaidSoft, no lo que pueda llegar por otra vía al tópico. La corrección del lado del agente está en el código del mismo commit, sin compilar ni distribuir | **Crítica** | Alto (LocalSystem en todas las estaciones) | Media: exige publicar en el tópico, cosa que pueden la credencial del panel, la API Key y el dashboard | Confirmado en código; versiones en producción el 19-oct. **Abierto** hasta distribuir un agente con la corrección y la firma obligatoria (0.37) |
| S-02 | **Dashboard de EMQX accesible desde otra red, por HTTP**, en 8082. Quien entra puede publicar en cualquier tópico y cambiar las ACL | **Alta** | Alto | Media | Confirmado en producción el 19-oct. El compose sigue publicando `8082:18083` en todas las interfaces. R-1 sin hacer |
| S-03 | **API Keys de EMQX sin vencimiento**, presentes en 5 contenedores; la API REST no pasa por las ACL. Hay **dos** activas, ambas con `expired_at: infinity`:
- `saidsoft-provisioning`: **la usa el `.env`**, pero no la crea ningún script del repositorio;
- `saidsof_django_admin`: **no la usa el `.env`**. Es la que crea `deploy/bootstrap-emqx.sh:170`, que solo la crea si no existe | **Alta** | Alto | Baja | Confirmado en producción el 19-oct y el 20-oct. R-8 sin hacer. Desactivar `saidsof_django_admin` (no borrarla, para que el script no cree otra) y alinear el script con la clave real |
| S-04 | **La credencial compartida de los agentes puede leer cualquier respuesta de enrolamiento**, que lleva token, secreto HMAC y contraseña MQTT (P0-2) | **Alta** | Alto (suplantar una estación) | Baja: hay que tener esa credencial | Confirmado en producción el 19-oct |
| S-05 | **El token de la API se obtiene sin MFA.** `/api/v1/auth/token/` es `obtain_auth_token` de DRF: usuario y contraseña bastan, aunque el usuario tenga MFA en el panel. Axes sí bloquea los intentos fallidos | **Media** | Medio: acceso a la API móvil y a la API RMM de solo lectura con los permisos del usuario | Baja | Confirmado en código (`config/urls.py:12`, `cuentas/forms.py`). **No reproducido** |
| S-06 | **Los tokens de la API no vencen** (`rest_framework.authtoken`) | **Media** | Medio | Baja | Confirmado en código |
| S-07 | **El MFA es opcional:** solo se exige a quien configuró un dispositivo (`LoginConOTPForm.clean`) | **Alta** | Alto | Media | Confirmado en código. **Confirmado en producción el 20-oct: ninguna de las 3 cuentas con privilegios (superusuario, Administrador u Operador RMM) tiene MFA** (`diego.aguilar`, `prueba`, `romo`). La cuenta **`prueba`** está activa y con privilegios |
| S-08 | **MeshCentral publicado en 8083 con imagen `:latest`**: da escritorio y terminal remotos | **Media** | Alto | Baja | Confirmado en producción el 19-oct; el contenedor huérfano ya no aparece el 20-oct. Imagen sin versión fija [CÓDIGO]; corre **MeshCentral 1.2.4** [PROD 20-oct] |
| S-09 | **`/media/` público por HTTP en 8080**: paquetes del POS, instaladores y el ejecutable del agente se descargan sin credenciales. La integridad la cubre el SHA-256 firmado; la confidencialidad no. `mantenimiento/` sí está protegido (`internal`) | **Media** | Medio (software propietario del POS) | Media dentro de la red | Confirmado en código (`deploy/nginx/nginx.conf`, `config/urls.py`) |
| S-10 | **El listener `ws:default` de EMQX sigue activo** dentro del contenedor, sin publicar | **Baja** | Bajo | Baja | Confirmado en producción el 19-oct |
| S-11 | **Un único secreto de flota (`COMANDO_HMAC_SECRET`) firma los catálogos**; los comandos ya usan un secreto por estación | **Media** | Alto si se filtra | Baja | Confirmado en código (`servicios_pos.envolver_firmado`) |
| S-12 | **Control total de la flota desde el servidor**: scripts PowerShell como LocalSystem, claves BitLocker cifradas con una clave que está en el mismo host | **Alta**, por diseño | Alto | Depende de la seguridad del NUC | `capacidad-2030.md` §7. Seguridad física del NUC: **NO VERIFICADA** |
| S-13 | **El NUC no filtra ningún puerto publicado.** `DOCKER-USER` vacía en IPv4 e IPv6 y `ufw` inactivo: 8080, 8081, 8082 (dashboard de EMQX), 8083 (MeshCentral), 8084 y 8085/UDP responden a cualquier red que llegue al servidor. Un firewall de red externo al NUC sigue sin verificar | **Alta** | Alto | Media | **Confirmado en producción el 20-oct** (`iptables -S DOCKER-USER`, `ip6tables`, `ufw status`) |

## 2. Estabilidad y consistencia

| ID | Hallazgo | Criticidad | Estado y evidencia |
|---|---|---|---|
| E-01 | **Los reportes del agente van con QoS 0 y sin reintento.** Si se corta la red, un paso de una instalación, de un despliegue o de un script se pierde y el panel queda en "pendiente" | **Alta** | Confirmado en código (`agente_prueba.py:949`). La suscripción con QoS 1 del worker no cambia nada |
| E-02 | **Software y despliegues se publican por MQTT 3.1.1 con QoS 0**: un rechazo de EMQX pasa como éxito | **Alta** | Confirmado en código (D-1 de la matriz) |
| E-03 | **Techo de 1024 conexiones en EMQX** (listener y `ulimit`) | **Alta** al crecer | Confirmado en producción el 19-oct, con 71 conexiones el 20-oct. No está declarado en el compose |
| E-04 | **Sin límites de recursos ni réplicas** en ningún contenedor | Media | Confirmado en código (`capacidad-2030.md` §5) |
| E-05 | **Worker MQTT de un hilo**, que procesa dentro del callback | Media hoy; Alta a escala | Confirmado en código |
| E-06 | **Condiciones de carrera posibles** CR-1 a CR-4: guardado completo del latido, alerta duplicada, reintento de despliegue, retiro del retenido | Media / Baja | HIPÓTESIS con base en el código (`AUDITORIA_FLUJO_ESTACION.md` §11) |
| E-07 | **Solicitudes y despliegues sin caducidad**: pueden quedar en `publicando` para siempre (C-11) | Media | Confirmado en código; 3 solicitudes así [PROD 19-oct] |
| E-08 | **Comandos de consulta sin seguimiento**: si no llega respuesta, nadie se entera | Baja | Confirmado en código |
| E-09 | **Dependencias sin versión fija** (`requirements.txt` con `>=`) y sin archivo de bloqueo | Media | Confirmado en código |
| E-10 | **Python 3.13 en producción y 3.14 en local** | Baja | Confirmado en código (`deploy/Dockerfile:3`; `CLAUDE.md`) |
| E-11 | **El correo de alertas no funciona** (`EMAIL_HOST_USER` vacía): Telegram es el único canal | Media | Confirmado en producción el 28-sep |

## 3. Gestión de secretos y configuración

- **Los secretos viven solo en `deploy/.env`**, fuera de git [CÓDIGO]. Bien.
- **Faltan 22 variables en la plantilla** `deploy/.env.prod.example`: Telegram, correo, WhatsApp,
  IA, NetFlow y dos de cookies y SSL [CÓDIGO]. Lista completa en `INVENTARIO_TECNICO.md`.
- **Credenciales cifradas en la base:** claves BitLocker y comunidades SNMP con Fernet
  (`catalogo/crypto.py`). La clave está en el `.env` del mismo host [CÓDIGO].
- **Producción** (`config/settings/produccion.py`): `DEBUG=False`, SSL forzado, cookies seguras,
  HSTS de 7 días con `preload`, sesión de 8 horas que se cierra al cerrar el navegador, Axes con 5
  intentos y 1 hora de bloqueo [CÓDIGO].

## 4. Separación entre organizaciones (unidades de negocio)

- **Mecanismo único:** `apps/cuentas/services.py` (`scope_por_unidad_negocio`,
  `verificar_acceso`, `scope_opcional_*`). Lo usan el panel, la API móvil y la API RMM [CÓDIGO].
- **API móvil:** acotada por `tecnico=request.user`, y por unidad de negocio en equipos [CÓDIGO].
- **API RMM** (`d063c75`): mismo alcance; devuelve 404 a lo ajeno. Cubierto por pruebas [CÓDIGO].
- **Fuga entre clientes: no se encontró.** La auditoría por agentes del 10-oct
  (`docs/auditoria/fase4-hallazgos.md`, `fase5-triage.md`) revisó el panel; esta revisión **no
  repitió** ese barrido vista por vista. **NO VERIFICADO de nuevo.**

## 5. Agente

- Corre como **LocalSystem** [DOC].
- **Órdenes firmadas con HMAC por estación**:
  - comandos y scripts con una ventana de 120 s;
  - órdenes retenidas sin ventana, con identificador de una sola vez en `identidad.json`.
- **0.29 y 0.32 aceptan firmas inválidas** (C-4); en producción el 19-oct había 2 y 9 estaciones
  con esas versiones.
- **Defectos del despliegue del POS** (alcance §11):
  - cierre forzado con `taskkill /F`;
  - rollback que no borra los archivos nuevos;
  - extracción del `.zip` sin validar rutas en la rama con carpeta raíz;
  - una pausa que no detiene lo ya entregado.

## 6. Trazabilidad y errores

- **Acciones humanas:** `auditoria.registrar_evento`, con IP; la usan 8 apps [CÓDIGO].
- **Mensajes MQTT que fallan:** `MensajeMqttFallido`, visible en el panel [CÓDIGO].
- **Enrolamientos rechazados:** `EnrolamientoRechazado`, en su bandeja [CÓDIGO].
- **Salud de la plataforma:** latidos del worker, de MeshCentral, de Beat, del respaldo y de la
  copia externa; temperatura. `verificar_salud` corre con un timer de systemd y en los
  healthchecks [CÓDIGO].
- **Logs:** el `stdout` de los contenedores (gunicorn con access log) y journald para los timers.
  **No hay retención ni centralización declaradas** [CÓDIGO].

## 7. Migraciones y operaciones destructivas

- Las migraciones que borran tablas **verifican que estén vacías y cortan el despliegue** si no
  lo están (`catalogo/0033_baja_de_integraciones`) [CÓDIGO].
- Los comandos de escritura masiva **simulan por defecto** y piden `--aplicar` [DOC + CÓDIGO].
- `Activo.delete()` lanza `NotImplementedError` [DOC].
- Acciones peligrosas con **cuatro ojos**: aprobar despliegues y scripts [CÓDIGO].
- **Freno de emergencia:** pausa por estación o de toda la flota (`pausar_flota`) y apagado de las
  altas [CÓDIGO].

## 8. Requisitos de recuperación detectados

Sin implementar nada, según la sección 7 del pedido.

| # | Requisito | Situación al 20-oct |
|---|---|---|
| RR-1 | Copia de la base **fuera del NUC** | Respaldo diario cifrado, **solo local** (14 días) [PROD 20-oct] |
| RR-2 | **Prueba de restauración** | **Nunca se hizo.** `pg_dump` avisa en cada corrida sobre las restricciones circulares de TimescaleDB [PROD 20-oct] |
| RR-3 | Respaldo de **EMQX**: usuarios, credenciales de las 85 estaciones y ACL | **Se respalda desde el 20-oct** (`68d0045`, desplegado): respaldo manual de las 19:46 con `emqx_…tar.gz.gpg` de 14 KB, cifrado, solo local [PROD 20-oct]. **Restaurarlo (`emqx ctl data import`) no se probó** |
| RR-4 | Respaldo de **MeshCentral** (`meshcentral_data`, `meshcentral_files`) | **No se respalda** [CÓDIGO] |
| RR-5 | **El `.env` y la passphrase de GPG** guardados fuera del NUC | **Hecho el 20-oct**: paquete cifrado en KeePass, con segunda copia en USB. Hay que rehacerlo cada vez que cambie el `.env` |
| RR-6 | **Plantilla del `.env` completa** | Faltan 22 variables |
| RR-7 | Respaldo de los **certificados TLS** (`deploy/certs`) | **Hecho el 20-oct**, en el mismo paquete que RR-5 |
| RR-8 | **Procedimiento escrito para reconstruir** el host: orden de servicios, bootstrap de EMQX, verificación | Parcial en `deploy/README-produccion.md` |
| RR-9 | **RPO y RTO definidos** | No están definidos |

**Sobre `68d0045`:** ese commit, ya en `origin/master`, incluye la subida a OneDrive con `rclone`
y el respaldo de EMQX. La subida a OneDrive solo funciona si `BACKUP_ONEDRIVE_REMOTO` está definido
en el `.env`. Por la sección 7 del pedido, **no se configuró ni se configurará en esta fase**.
`rclone` quedó instalado en el NUC por el usuario antes de este pedido.

---

## 9. Cerrado desde las auditorías anteriores

| ID | Qué | Commit / fecha |
|---|---|---|
| R-2 | Catálogos publicados, firmados y retenidos | 20-oct, en producción |
| R-4 (actualizaciones) | Borrado del retenido por Celery con MQTT 5 | `b99fa53` |
| R-4 (software y despliegues) | Borrado del retenido al confirmar la recepción | `688e104` |
| R-6 | Retainer en disco; sobrevivió a un reinicio completo | 20-oct, en producción |
| P0-1, **solo la parte del servidor** | Validación del proveedor en `EventoSistemaVigilado` (migración `0049`). La consulta de eventos sin interpolación está en `agente_prueba.py` del mismo commit, pero **ese agente no se compiló ni se distribuyó**: P0-1 sigue abierto (S-01) | `01f982b` |
| Salud | Temperatura del servidor como componente | `6e0df91` |
| Worker de MeshCentral | Informa si está conectado, no solo si está vivo | `594ccea` |
