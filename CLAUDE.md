# Instrucciones para trabajar en saidsoft-core

## Qué es esto y dónde está parado (traspaso — 7-sep-2026)

Plataforma **RMM + ITAM + servicio en campo** para CRESIO (Django 5.2 / Python 3.14,
panel HTMX, app móvil Flutter, multi-cliente por `UnidadNegocio`). Tres unidades de
negocio: SG (393 farmacias), MIA (305), 7DIAS (2).

**El estado real en una línea: la plataforma está construida; el despliegue no.**
15 apps Django, 2.119 pruebas, 701 farmacias modeladas — y **66 equipos con agente
instalado de ~1.800 estimados**. El cuello de botella dejó de ser el código.

Los tres números se corrigieron el 5-oct-2026 contra la base de producción (decía
"~832 pruebas, 700 farmacias, 8 equipos con agente"). 66 estaciones aprobadas, 55 con
latido en las últimas 24 h. El rollout avanzó 8x respecto de lo que este archivo
afirmaba, y eso cambia el orden de magnitud de varias cosas: ver §10-AX del plan y el
informe de auditoría de esa fecha para las proyecciones hacia 1.300 farmacias.

### Producción

- Servidor `10.111.6.20` (Intel NUC en sitio). Panel en `https://10.111.6.20:8084/`,
  media sin auth en `http://10.111.6.20:8080/media/`.
- Repo desplegado en `~/Documentos/Said/saidsoft-core`; stack en `deploy/` con
  **`docker compose` v2** (contenedores `deploy-web-1`, con guiones).
- Acceso SSH con usuario `glpi`. **Las credenciales NO están en el repo** — pedírselas
  al usuario. Lo mismo para todo secreto: viven solo en el `deploy/.env` del servidor.
- Despliegue: `git pull` → `docker compose --env-file .env build` → `up -d`.
  El entrypoint corre las migraciones solo.
  **`build web` NO alcanza** (decía eso acá hasta el 11-sep-2026): los servicios
  comparten el mismo `Dockerfile` vía el ancla `x-app`, pero Compose genera **una
  imagen por servicio** — hoy **seis**: `deploy-web`, `deploy-worker`,
  `deploy-celery_beat`, `deploy-celery_worker`, `deploy-meshcentral_worker` y
  `deploy-telegram_bot` (esta última faltaba en esta lista hasta el 6-oct-2026, y es
  donde vive el bot de Telegram). Con `build web` solo se
  recrea `web` y el worker MQTT y Celery se quedan con el código viejo — pasó en el
  despliegue del 11-sep: beat seguía sin la tarea nueva y sin el `ping` del
  Dockerfile. `build` sin argumento las construye todas.
  **Para comprobar que ninguna quedó con código viejo** (6-oct-2026), comparar el archivo
  DENTRO del contenedor contra el checkout:
  `docker exec deploy-worker-1 cat /app/apps/monitoreo/services.py | diff -q - apps/monitoreo/services.py`.
  Es exacto y no depende de elegir bien una palabra a buscar ni de interpretar fechas de
  imagen.
- **Un cambio a `deploy/nginx/nginx.conf` exige RECREAR el contenedor, no recargarlo**:
  es un bind mount de archivo suelto y ata el inode, así que `git pull` no lo alcanza
  (ver §10-AA).

### Entorno local (probar antes de desplegar)

Ver `deploy/README-local.md`. Resumen: base y Redis en Docker con el **mismo motor que
producción**, Django nativo con `runserver`.

**Correr las pruebas contra PostgreSQL, no SQLite.** No es preferencia: probar en un
motor y desplegar en otro ya escondió una familia entera de bugs — 125 pruebas que
pasaban en SQLite fallaban en PostgreSQL (§10 del plan), sumas infladas por JOIN en
viáticos, y `ip_lan='localhost'` que SQLite acepta y el tipo `inet` rechaza. El CI ya
corre sobre `timescale/timescaledb`.

### Lo que bloquea de verdad

1. **~~Sin ruta de red a las farmacias.~~ DESACTUALIZADO — verificado el 11-sep-2026.**
   Esto decía que el servidor no alcanza las IP privadas de las farmacias (100% de
   pérdida de ping, 24-ago-2026). **Ya no es cierto.** Comprobado sobre el NUC real:
   - Desde el host: 19 de 25 gateways de San Gregorio responden al ping. Las 6 que no
     son caídas reales o sitios de baja — sin ruta fallarían las 25.
   - Desde adentro del contenedor de Celery: `connect()` TCP a esas IP devuelve
     **ConnectionRefused**, que prueba que el paquete llegó y volvió. El ICMP fallaba ahí
     por otra causa: la imagen no traía `iputils-ping` (corregido en `deploy/Dockerfile`).

   **Ojo con la conclusión:** lo verificado es la dirección **servidor → farmacia**. La
   otra mitad de la frase original (que un agente en farmacia llegue al broker) es la
   dirección contraria y NO se probó acá — aunque las 8 estaciones que hoy reportan por
   MQTT sugieren que funciona. **Antes de rearmar el plan de rollout alrededor de esto,
   conviene entender por qué cambió y si es permanente**: nadie registró el cambio, y el
   NUC sale por WiFi (`wlo1`) con ruta por defecto, no por una VPN dedicada.
2. **El instalador nunca se usó a escala.** Las primeras 8 estaciones se hicieron a mano
   y cada una destapó un bug distinto.
   **Ojo con este punto — 5-oct-2026:** hoy hay **66 estaciones enroladas**, no 8. Lo
   que NO se verificó es *cómo* se instalaron las otras 58: si fue con el instalador, la
   frase de arriba ya no vale y conviene reescribirla con lo que se aprendió; si también
   fueron a mano, el pendiente es más grande de lo que dice. Preguntarle a quien las
   instaló antes de planificar el rollout alrededor de esta frase.
3. **Los módulos están vacíos.** 22 activos, 10 colaboradores, **0 zonas de viáticos
   cargadas** (medido el 5-oct-2026; decía "9 activos, 9 colaboradores") — sin zonas, la
   alerta "fuera de zona" no se dispara nunca, y eso sigue siendo cierto. Se construye
   más rápido de lo que se pone en uso: ese es el riesgo real del proyecto, no lo
   técnico.
   Para dimensionarlo: 22 activos contra 701 farmacias y 66 estaciones con agente. El
   módulo de ITAM está prácticamente sin usar, así que el cruce `activo` ×
   `dispositivo_detectado` ("qué hay enchufado que nadie inventarió") no tiene contra qué
   cruzar.

### Pendientes concretos

- **2 estaciones apagadas** (`MC001-B`, `MC001-C`) siguen con la credencial MQTT
  compartida — verificado contra EMQX el 21-sep-2026: no existe usuario propio para
  ellas. `ML016-A` **ya migró** (decía lo contrario acá hasta hoy: está conectada como
  `usuario=ML016-A`). Cuando enciendan, correrles el script "Migrar a credencial MQTT
  propia".
  **La ROTACIÓN de credencial estaba rota; corregida en código el 28-sep-2026** (§10-AV):
  el `PUT` de `apps.mqtt_worker.emqx_admin` mandaba `user_id` en el cuerpo y EMQX 5.8.3
  responde HTTP 400 `unknown_fields`, así que `aprovisionar_credencial_estacion` devolvía
  None para cualquier estación que ya existiera. Crear una nueva siempre funcionó
  (POST 201). **Falta comprobarlo contra el broker real**: el arreglo tiene prueba que
  falla con el cuerpo viejo, pero lo que lo cierra es un re-enrolamiento contra EMQX
  5.8.3 (runbook en `deploy/README-produccion.md`). Hacer esa comprobación antes de rotar
  `MQTT_PASSWORD_AGENTE` / `COMANDO_HMAC_SECRET` y de correr
  `deploy/emqx-narrow-acl-agente.sh`.
- ~~**El CI nunca se vio en verde** tras pasarlo a TimescaleDB.~~ **RESUELTO el
  21-sep-2026.** El diagnóstico de acá era erróneo: no tenía que ver con TimescaleDB y el
  CI sí estuvo verde hasta el 15-sep 03:34 (`3c9c42c0`). Lo rompió `95720f2`, que agregó
  `armar_paquete_agente` y pruebas que leen `deploy/certs/cert.pem` — un archivo
  **ignorado por git**, que existe en la máquina de quien desarrolla y no en un checkout
  limpio. 38 días en rojo, verde en local todo el tiempo.
  La lección general: **una prueba que lee algo ignorado por git no puede pasar en CI.**
- ~~**149 farmacias sin circuito de proveedor.**~~ **RESUELTO — medido el 5-oct-2026:**
  es **1 de 701**. `circuito_proveedor` está cargado en 700 farmacias. Quedan los 3
  códigos de planilla que no existen en SAIDSOFT (`MCMB-2`, `MMIL10`, `MPREV1`), que son
  otro problema y no se verificaron en esa pasada.
- **Respaldos sin copia fuera del servidor** (diarios, cifrados con GPG, retención 14
  días — pero en la misma máquina que respaldan).
- **Push FCM real**: falta registrar la app en Firebase. El nombre de paquete es
  `com.cresio.cresio_campo`, **no** `com.cresio.campo` como decía este archivo hasta el
  9-sep — y cambiarlo ya no es gratis (ver §10-AH del plan).
- ~~**Respaldo del keystore fuera de esta máquina.**~~ **HECHO el 26-sep-2026.** Era el
  único pendiente irreversible del proyecto. La herramienta es
  `movil-campo/android/respaldar-keystore.sh` (§10-AR): cifra con GPG, comprueba la
  huella antes de empaquetar y `verificar` prueba que la copia se puede restaurar.
  Correr `verificar` sobre la copia ya movida, cada tanto y después de cualquier cambio
  de máquina: un respaldo que nunca se restauró es una suposición, no un respaldo.
- **APK**: el release ya se firma con el keystore de CRESIO (§10-AH) y desde el
  26-sep-2026 hay `publicar_apk` + `/api/v1/version-app/`, así que la app **avisa** que
  hay versión nueva (§10-AT). La distribución en sí **sigue siendo manual**: se copia a
  `/media/movil/` y alguien lo instala teléfono por teléfono. Y hay **seis cambios
  acumulados sin distribuir** (hora real de la cola offline, clave de idempotencia,
  revocar el consentimiento de ubicación, catálogo unificado, orden por urgencia desde el
  servidor y el propio aviso de versión). Ojo con la vuelta: los teléfonos que hoy están
  en la calle **no tienen el aviso**, así que de esta tanda se enteran como siempre.
- **Certificado TLS**: `deploy/verificar-certificado.sh` compara lo que sirve el servidor
  contra lo que empaqueta la app y avisa si vence pronto. Si hay que rotar, el orden es
  **primero el APK, después el servidor** — al revés deja a toda la flota de teléfonos
  fuera de servicio (runbook en `deploy/README-produccion.md`).
- Decisiones abiertas del usuario: **crear visitas desde la app** (hoy el ViewSet es de
  solo lectura) y **abrir el mapa hacia la farmacia** (el backend ya manda coordenadas
  y nada las usa).

### Cómo se trabaja acá

- Los comandos de management que escriben en masa **simulan por defecto** y exigen
  `--aplicar`. Seguir ese patrón (`importar_circuitos_proveedor`,
  `crear_activos_desde_rmm`).
- Antes de borrar datos o archivos, mirarlos. Un inventario de "huérfanos" con los
  nombres de campo equivocados marcó como sobrantes el agente vigente y los
  instaladores de software; borrarlos habría roto los despliegues.
- Nunca versionar secretos ni imprimirlos. `deploy/.env`, `key.properties` y los
  `.jks` quedan fuera de git.

## Arquitectura: dónde está cada cosa

El mapa completo (stack con versiones, 95 modelos, flujos, convenciones, entorno de
pruebas) está en **`docs/auditoria/fase1-descubrimiento.md`**. Leelo antes de trabajar
sobre un módulo que no conocés; acá va solo lo que hay que tener a mano siempre.

15 apps Django bajo `apps/`. La lógica de negocio vive en `apps/<app>/services.py`, nunca
en las vistas. `apps/panel` es presentación de todo el sistema y no tiene modelos.
Tres componentes separados: backend (`saidsoft-core`), agente Windows (`agente-prueba/`)
y app Flutter (`movil-campo/`).

## Regla de reutilización

Antes de crear una implementación nueva: **buscar si ya existe**, identificarla, evaluar
si sirve, y proponer una nueva solo si la existente no alcanza — explicando por qué.
Si ya existe una implementación para una función, documentala; no propongas otra.

| Necesidad | Reusar |
|---|---|
| SNMP al equipo de borde (ancho de banda, identidad, ARP) | `apps/monitoreo/mikrotik.py` |
| SNMP a cualquier otro dispositivo (impresora, switch, UPS) | `apps/monitoreo/snmp/` — para un tipo nuevo, **una entrada en `catalogo.py`**, no un módulo nuevo |
| Alertas por estación | `apps/monitoreo/services.py` (`ReglaAlerta`/`Alerta`) |
| Alertas por sitio/enlace | `apps/monitoreo/enlaces.py` |
| Cifrado de credenciales | `apps/catalogo/crypto.py` (Fernet) |
| Inventario físico | `apps/activos/models.py` → `Activo` |
| Trazabilidad de un activo | `EventoActivo` |
| Auditoría de acciones humanas | `apps/auditoria` → `registrar_evento()` |
| Tareas periódicas | Celery / `CELERY_BEAT_SCHEDULE` en `config/settings/base.py` |
| Observación por ARP | `apps/monitoreo/models.py` → `DispositivoDetectado` |

**No crear arquitecturas paralelas.**

## Reglas de dominio que no se cambian sin evidencia

- **`Activo` es la entidad central del inventario físico.** No crear un modelo paralelo
  para el mismo equipo. `Activo` nunca se elimina: `delete()` lanza `NotImplementedError`.
- **Inventario declarado ≠ observado.** `Activo` es lo que alguien declaró;
  `DispositivoDetectado` (ARP) y `EquipoBordeFarmacia` (SNMP) son lo que la red reporta.
  El cruce entre los dos es el producto, no una duplicación a eliminar.
- **Una sola fuente de verdad para la IP.** Si el activo tiene `estacion` vinculada, manda
  `Estacion.ip_lan` (la reporta el agente); si no, `Activo.ip` cargada a mano. Lo resuelve
  `Activo.ip_efectiva`, y `Activo.clean()` impide cargar las dos a la vez.
- **Estados de inventario y de monitoreo están separados.** `Activo.Estado` (en_bodega /
  asignado / en_reparacion / dado_de_baja) no representa conectividad;
  `Estacion.EstadoConexion` no representa custodia. No mezclarlos.
- **Tres niveles de ubicación, deliberadamente separados.** `Activo.farmacia` = en qué
  sitio de la red está. `Activo.ubicacion` → `Ubicacion` (agencia/sede con dirección y
  coordenadas, la misma que usan las visitas técnicas y los colaboradores) = dónde está
  cuando NO está en farmacia ni en bodega: matriz, oficinas. `UbicacionInterna` (choices) =
  dónde está montado dentro de la farmacia. No colapsarlos.
- **Los secretos no se leen ni se imprimen.** `.env` y `deploy/.env` quedan fuera de git y
  fuera de cualquier lectura. Para entender la configuración están `.env.example` y
  `deploy/.env.prod.example`.

## Límites de los agentes de auditoría

Los agentes de `.claude/agents/` **auditan; no corrigen**. No modifican código,
migraciones ni datos; no ejecutan pruebas ni `manage.py shell`; no se conectan a ninguna
base de datos; no hacen commits ni operaciones de git que escriban. Entregan hallazgos con
evidencia (archivo, línea, función) para que un humano decida. Ningún hallazgo se marca
CONFIRMADO por inferencia: si hace falta ejecutar algo, se marca pendiente de reproducción.

Las restricciones de lectura y de shell están en `.claude/settings.json`. Las reglas `deny`
sobre `Bash` son una **lista negra, no un sandbox**: acotan los comandos escritos de la
forma habitual, no cierran toda vía posible. El control estructural real es el `tools:` de
cada agente — `revisor-bd` y `revisor-inventario` no reciben `Bash` en absoluto.

## Mantener la documentación al día

`README.md` y `PLAN_MODERNIZACION.md` son documentación **viva**, no una foto del
día que se escribieron. Cada vez que se cierra un cambio de alcance real (una fase
nueva, un modelo nuevo, un módulo nuevo, un comando de management nuevo, un cambio de
comportamiento que alguien notaría), actualiza el archivo correspondiente **en el
mismo turno de trabajo**, no como tarea aparte para después:

- **`PLAN_MODERNIZACION.md`**: la fase/feature en la tabla de la sección
  correspondiente (§7 fases originales, §9 fases RMM, §10 auditoría de pendientes).
  Si una fase pendiente de §9/§10 se completa, muévela a "hecho" con una nota breve de
  qué se implementó y en qué archivos vive la lógica principal.
- **`README.md`**: si el cambio afecta cómo alguien arranca el proyecto, qué apps
  existen, o cómo se usa una feature del panel, actualiza la sección correspondiente
  (o agrega una nueva, siguiendo el estilo de las existentes: qué es, por qué, dónde
  vive el código).

Si un docstring en el código queda desactualizado por el cambio (ej. dice "Fase 2,
pendiente" de algo que ya se implementó), corrígelo de una vez — no dejes referencias
a fases futuras que ya pasaron.

Esto surgió porque el 31-jul-2026 ambos documentos quedaron varias fases atrás del
código real (no mencionaban multi-tenancy, alertas, scripts programados, RBAC por
cliente ni reportes por cliente) y hubo que reconstruir el estado a mano. No se debe
repetir.

## Verificación antes de dar por terminado un cambio

- `python manage.py check`
- `python manage.py makemigrations --check --dry-run` (sin drift de migraciones)
- `python manage.py test` (suite completa; el venv está en `.venv/`)
