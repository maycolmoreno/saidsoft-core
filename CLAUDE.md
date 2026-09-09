# Instrucciones para trabajar en saidsoft-core

## Qué es esto y dónde está parado (traspaso — 7-sep-2026)

Plataforma **RMM + ITAM + servicio en campo** para CRESIO (Django 5.2 / Python 3.14,
panel HTMX, app móvil Flutter, multi-cliente por `UnidadNegocio`). Tres unidades de
negocio: SG (393 farmacias), MIA (305), 7DIAS (2).

**El estado real en una línea: la plataforma está construida; el despliegue no.**
16 apps Django, ~832 pruebas, 700 farmacias modeladas — y **8 equipos con agente
instalado de ~1.800 estimados**. El cuello de botella dejó de ser el código.

### Producción

- Servidor `10.111.6.20` (Intel NUC en sitio). Panel en `https://10.111.6.20:8084/`,
  media sin auth en `http://10.111.6.20:8080/media/`.
- Repo desplegado en `~/Documentos/Said/saidsoft-core`; stack en `deploy/` con
  **`docker compose` v2** (contenedores `deploy-web-1`, con guiones).
- Acceso SSH con usuario `glpi`. **Las credenciales NO están en el repo** — pedírselas
  al usuario. Lo mismo para todo secreto: viven solo en el `deploy/.env` del servidor.
- Despliegue: `git pull` → `docker compose --env-file .env build web` →
  `up -d`. El entrypoint corre las migraciones solo.
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

1. **Sin ruta de red a las farmacias.** El servidor no alcanza sus IP privadas; sin VPN
   ningún agente en farmacia llega al broker. Es el bloqueante nº 1 del rollout y el
   único que no se resuelve con código.
2. **El instalador nunca se usó a escala.** Las 8 estaciones se hicieron a mano y cada
   una destapó un bug distinto.
3. **Los módulos están vacíos.** 9 activos, 9 colaboradores, 0 zonas de viáticos
   cargadas — sin zonas, la alerta "fuera de zona" no se dispara nunca. Se construye
   más rápido de lo que se pone en uso: ese es el riesgo real del proyecto, no lo
   técnico.

### Pendientes concretos

- **3 estaciones apagadas** (`MC001-B`, `MC001-C`, `ML016-A`) siguen con la credencial
  MQTT compartida. Cuando enciendan, correrles el script "Migrar a credencial MQTT
  propia". Recién después tiene sentido rotar `MQTT_PASSWORD_AGENTE` /
  `COMANDO_HMAC_SECRET` y correr `deploy/emqx-narrow-acl-agente.sh`.
- **El CI nunca se vio en verde** tras pasarlo a TimescaleDB. Revisar la pestaña
  Actions.
- **149 farmacias sin circuito de proveedor** y 3 códigos de planilla que no existen
  en SAIDSOFT (`MCMB-2`, `MMIL10`, `MPREV1`).
- **Respaldos sin copia fuera del servidor** (diarios, cifrados con GPG, retención 14
  días — pero en la misma máquina que respaldan).
- **Push FCM real**: falta registrar la app en Firebase. El nombre de paquete es
  `com.cresio.cresio_campo`, **no** `com.cresio.campo` como decía este archivo hasta el
  9-sep — y cambiarlo ya no es gratis (ver §10-AH del plan).
- **APK**: el release ya se firma con el keystore de CRESIO (§10-AH). Queda el **respaldo
  del keystore fuera de esta máquina** — si se pierde, ninguna app instalada se puede
  actualizar nunca más — y el **canal de distribución**: se sigue copiando a mano a
  `/media/movil/` y la app no avisa que hay versión nueva.
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
