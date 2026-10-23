# Migración del servidor SAIDSOFT

Procedimiento para mover SAIDSOFT del NUC (`10.111.6.20`) a otro servidor sin perder
datos ni la conexión de los agentes. Hay dos etapas, y se pueden hacer en momentos
distintos:

- **Etapa A — servidor nuevo con la misma IP.** Los agentes no se enteran. Se puede hacer
  hoy.
- **Etapa B — pasar a un subdominio.** Necesita preparar a los agentes antes (ver más
  abajo). Se hace cuando el subdominio exista.

Escrito el 22-oct-2026. Antes de usarlo, revisar que los nombres de volúmenes, puertos y
scripts sigan siendo los de `deploy/docker-compose.yml`.

---

## Por qué hay que tener cuidado

Cada estación tiene grabados, en su `config.json`:

- **la dirección del servidor**: `host = 10.111.6.20`, puerto `8081` (MQTT sobre TLS);
- **el certificado en el que confía**: `cert.pem`, autofirmado (`CN=saidsof-mqtt`).
  El agente rechaza cualquier servidor que no presente ese certificado, o uno firmado
  con la misma clave, para la dirección a la que se conecta.

Y el servidor guarda, fuera de la base de Django:

- en **EMQX**, el usuario y la contraseña MQTT propios de cada estación (se crean al
  enrolarla). Sin ellos ninguna estación se vuelve a conectar, aunque la base esté
  completa;
- en **`deploy/.env`**, las claves que descifran o firman datos.

Ninguna de estas cosas se puede rehacer desde el panel. Si se pierden, hay que ir
equipo por equipo.

---

## Qué hay que llevar al servidor nuevo

| Qué | Dónde está en el NUC | Cómo se lleva | Si se pierde |
|---|---|---|---|
| Base de datos | volumen `db_data` | `backup.sh` → `db_*.sql.gz.gpg` | Todo el historial, inventario, órdenes y alertas. |
| EMQX (usuarios, credenciales de cada estación, ACL, retenidos) | volumen `emqx_data` | `backup.sh` → `emqx_*.tar.gz.gpg` (es un `emqx ctl data export`) | **Ninguna estación se conecta.** |
| Archivos subidos (instaladores, paquetes, ejecutables del agente, imágenes) | volumen `media_data` | `backup.sh` → `media_*.tar.gz.gpg` | No se pueden reinstalar ni desplegar versiones ya subidas. |
| MeshCentral (acceso remoto) | volúmenes `meshcentral_data` y `meshcentral_files` | **A mano** (ver paso A3): `backup.sh` no los incluye | Los agentes de MeshCentral dejan de conectarse: confían en el certificado guardado ahí. |
| Secretos | `deploy/.env` | Copia manual, y la copia de KeePass | Ver la tabla siguiente. |
| Certificado | `deploy/certs/cert.pem` y `key.pem` | Copia manual (la clave privada no está en git) | Ningún agente confía en el servidor nuevo. |

Variables de `deploy/.env` que **no se pueden regenerar**:

| Variable | Si se pierde |
|---|---|
| `BITLOCKER_ENCRYPTION_KEY` | Las claves de recuperación de BitLocker guardadas quedan ilegibles. |
| `CATALOGO_FIRMA_PRIVADA` | Los agentes 0.38 dejan de aceptar catálogos nuevos (siguen con el último). |
| `COMANDO_HMAC_SECRET` | Los agentes anteriores a 0.21 y los que no tienen secreto propio dejan de aceptar órdenes. |
| `BACKUP_ENCRYPTION_PASSPHRASE` | Los respaldos ya hechos no se pueden abrir. |
| `EMQX_COOKIE`, contraseñas de EMQX y del panel | Se pueden cambiar, pero hay que ajustar todo lo que las usa. |

Antes de empezar, comprobar que **todas** están en KeePass y que coinciden con las del
NUC.

---

## Etapa A — servidor nuevo con la misma IP

### A0. Antes (sin cortar nada)

1. Servidor nuevo con Ubuntu, Docker y Docker Compose v2. Mismos puertos abiertos que el
   NUC: `8080` (descargas de los agentes), `8081` (MQTT TLS), `8083` (MeshCentral),
   `8084` (panel), `8085/udp` (NetFlow).
2. Clonar el repo en la misma ruta: `~/Documentos/Said/saidsoft-core`.
3. Copiar `deploy/.env` y `deploy/certs/` desde el NUC (por `scp`, nunca por chat ni
   correo).
4. Comprobar que el certificado incluye la IP: los agentes se conectan a `10.111.6.20` y
   validan que esa IP esté en el certificado.
   ```
   openssl x509 -in deploy/certs/cert.pem -noout -subject -ext subjectAltName
   ```
   Tiene que aparecer `IP Address:10.111.6.20`. **No generar un certificado nuevo**: los
   agentes solo confían en este.
5. **Ensayo completo**: hacer los pasos A2 a A5 en el servidor nuevo, todavía con OTRA IP,
   usando el respaldo de la noche anterior. Comprobar que el panel abre y que los datos
   están. No se pueden conectar agentes a esa IP de prueba (el certificado no la
   incluye), pero sí verificar todo lo demás. Después, borrar los volúmenes del ensayo.

### A1. Ventana de corte (fuera de horario)

En el NUC:
```
cd ~/Documentos/Said/saidsoft-core/deploy
docker compose stop web worker celery_worker celery_beat meshcentral_worker telegram_bot netflow_collector
sh backup.sh /ruta/de/respaldos        # base, media y EMQX, cifrados
docker run --rm -v deploy_meshcentral_data:/d -v /ruta/de/respaldos:/r alpine tar czf /r/meshcentral_data.tar.gz -C /d .
docker run --rm -v deploy_meshcentral_files:/d -v /ruta/de/respaldos:/r alpine tar czf /r/meshcentral_files.tar.gz -C /d .
docker compose stop
```
Confirmar los nombres reales de los volúmenes con `docker volume ls` (el prefijo es el
nombre del proyecto de Compose, hoy `deploy_`).

Copiar los archivos de respaldo al servidor nuevo y verificar el SHA-256 de cada uno en
los dos lados.

### A2. Base de datos

En el servidor nuevo, con solo la base levantada:
```
cd ~/Documentos/Said/saidsoft-core/deploy
docker compose up -d db
sh restaurar-backup.sh /ruta/db_AAAAMMDD_HHMMSS.sql.gz.gpg <nombre_real_de_la_base>
```
El segundo argumento hace `DROP DATABASE` de la base indicada y la recrea, a propósito.
El nombre real está en `POSTGRES_DB` de `.env`. Hacerlo solo en el servidor nuevo.

### A3. Archivos y MeshCentral

```
# media (descifrar primero con la passphrase de .env)
gpg --decrypt /ruta/media_AAAAMMDD.tar.gz.gpg > /tmp/media.tar.gz
docker run --rm -v deploy_media_data:/d -v /tmp:/r alpine sh -c 'tar xzf /r/media.tar.gz -C /d'

# MeshCentral
docker run --rm -v deploy_meshcentral_data:/d -v /ruta:/r alpine sh -c 'tar xzf /r/meshcentral_data.tar.gz -C /d'
docker run --rm -v deploy_meshcentral_files:/d -v /ruta:/r alpine sh -c 'tar xzf /r/meshcentral_files.tar.gz -C /d'
```
El tar de media se arma con `tar czf - -C /app/media .` (ver `backup.sh`): se extrae en la
raíz del volumen, como arriba.

### A4. EMQX

El nodo se llama siempre `emqx@127.0.0.1` (`EMQX_NODE__NAME` en el compose). Cambiar ese
nombre equivale a empezar con EMQX vacío: no tocarlo.
```
docker compose up -d emqx
gpg --decrypt /ruta/emqx_AAAAMMDD.tar.gz.gpg > /tmp/emqx-export.tar.gz
docker compose cp /tmp/emqx-export.tar.gz emqx:/opt/emqx/data/backup/emqx-export.tar.gz
docker compose exec emqx emqx ctl data import /opt/emqx/data/backup/emqx-export.tar.gz
docker compose exec emqx emqx ctl admins list
```
Comprobar en el dashboard de EMQX (túnel SSH, ver `README-produccion.md`) que aparecen los
usuarios de las estaciones. El límite de 6000 conexiones del listener TLS y el `nofile`
del contenedor ya están en `docker-compose.yml`: viajan con el repo, no hace falta
reaplicarlos.

### A5. Levantar todo

1. Apagar el NUC o desconectarlo de la red. **No puede haber dos equipos con
   `10.111.6.20`.**
2. Asignar `10.111.6.20` al servidor nuevo.
3. Levantar el stack:
   ```
   docker compose up -d
   sh esperar-stack.sh
   ```

### A6. Verificar

```
docker compose exec web python manage.py showmigrations | grep '\[ \]'   # no debe mostrar nada
docker compose exec web python manage.py clave_firma_catalogos            # la misma pública de siempre
docker compose exec web python manage.py estado_catalogos
docker compose exec web python manage.py estado_mfa
sh verificar-certificado.sh
```
En el panel:
- en 5 a 10 minutos, las estaciones vuelven a "en línea" (los agentes reintentan solos);
- abrir una estación con MeshCentral (escritorio remoto);
- crear una solicitud de prueba a una estación de pruebas y ver que llega.

Republicar los catálogos para que el retenido salga del servidor nuevo:
```
docker compose exec web python manage.py publicar_catalogo_servicios_pos --aplicar
docker compose exec web python manage.py publicar_catalogo_eventos_sistema --aplicar
```

Reactivar los timers de systemd del host (`saidsoft-respaldo.timer`,
`saidsoft-salud.timer`) en el servidor nuevo: viven en el sistema operativo, no en
Docker. Ver `README-produccion.md`.

### A7. Vuelta atrás

El NUC queda **apagado e intacto** al menos una semana. Si algo falla: apagar el servidor
nuevo, volver a encender el NUC con su IP y levantar su stack. Lo que se haya hecho en
el servidor nuevo mientras tanto se pierde: por eso conviene decidir en las primeras
horas.

---

## Etapa B — pasar a un subdominio

### Dominio previsto — SIN CONFIRMAR

`saidsoft.elipsys.ec → 10.111.6.20` (registro `A`, TTL 300). Propuesto el 22-oct-2026;
todavía no está creado ni comprobado. Antes de usarlo en cualquier configuración:

- [ ] Quien administra el DNS de `elipsys.ec` confirma el nombre y crea el registro.
      Preferible en el DNS **interno** (el que usan las farmacias), no en el público: la
      IP es privada y publicarla muestra cómo está armada la red.
- [ ] Desde una estación de farmacia, por la VPN: `nslookup saidsoft.elipsys.ec` responde
      `10.111.6.20`. Probar en varias farmacias: el DNS que usan puede no ser el de la
      oficina.
      Prueba del 22-oct-2026 en una estación de farmacia: DNS `192.168.112.201`,
      respuesta "Non-existent domain" (el registro todavía no estaba creado). Ese es el
      DNS en el que tiene que aparecer el registro.
- [ ] Desde el NUC y desde una PC de oficina, lo mismo.
- [ ] Preguntar si se pueden crear registros `TXT` de validación
      (`_acme-challenge.saidsoft.elipsys.ec`). Con eso se puede sacar un certificado
      reconocido (por ejemplo Let's Encrypt validando por DNS, porque el servidor no es
      accesible desde internet): el navegador deja de advertir y el agente 0.39 puede
      confiar en él sin un certificado fijado propio.

Mientras no esté todo marcado, el nombre no se pone en `.env`, en el certificado ni en
los agentes.

### Qué falta hoy (a desarrollar antes)

No se puede pasar a un subdominio cambiando solo el servidor:

1. **Los agentes tienen la IP escrita** en `config.json` y no hay forma de cambiársela
   desde el panel. Falta, en una versión nueva del agente (0.39):
   - conectarse por nombre (`host` = el subdominio) y, si no responde, probar la IP;
   - una orden firmada "cambiar servidor" que actualice `config.json` desde el panel.
2. **El certificado está fijado.** El agente valida el certificado contra su `cert.pem`
   y comprueba que el nombre o la IP a la que se conecta estén en él. Para el subdominio
   hace falta un certificado que incluya el subdominio **y** `10.111.6.20` (para los
   agentes que todavía no cambiaron), y que los agentes acepten. Dos caminos, a decidir
   y **probar en laboratorio** antes:
   - **Reemitirlo con la misma clave y el mismo nombre** (`CN=saidsof-mqtt`, misma
     `key.pem`), agregando el subdominio al SAN. En principio los agentes lo aceptan
     porque está firmado con la clave en la que ya confían. Sin cambios en el agente,
     pero hay que confirmarlo con un agente real.
   - **Una autoridad certificadora interna**: se distribuye su certificado con el agente
     0.39 y desde entonces el certificado del servidor se puede renovar sin tocar las
     estaciones. Ojo: hoy la actualización automática del agente reemplaza solo el
     ejecutable, **no `cert.pem`**, así que la 0.39 tendría que traer el certificado
     dentro o reemplazarlo ella.
3. **La app de campo** (`movil-campo`) lleva una copia de `cert.pem` empaquetada. Si el
   certificado cambia, hay que publicar una versión nueva de la app (ver
   `verificar-certificado.sh`).
4. **MeshCentral**: sus agentes confían en el certificado guardado en `meshcentral_data` y
   se conectan a la dirección con la que se instalaron. Cambiar a subdominio implica
   ajustar `cert` en `deploy/meshcentral/config.json` y verificar que sus agentes sigan
   conectando (se puede mantener la IP como alternativa).

### Pasos, cuando lo anterior esté listo

1. Crear el registro DNS del subdominio apuntando a `10.111.6.20` (interno) y comprobar
   que **todas las farmacias** lo resuelven (el DNS de las farmacias puede no ser el de
   la oficina). Si alguna no lo resuelve, el agente 0.39 sigue por IP.
2. Reemitir el certificado con el subdominio y la IP en el SAN (camino elegido en el
   punto 2). Comprobar con `openssl x509 ... -ext subjectAltName`.
3. Probar con un agente de laboratorio y con 1 o 2 estaciones de prueba.
4. En `deploy/.env`:
   ```
   ALLOWED_HOSTS=<subdominio>,10.111.6.20,localhost
   CSRF_TRUSTED_ORIGINS=https://<subdominio>:8084,https://10.111.6.20:8084
   ARCHIVOS_BASE_URL=http://<subdominio>:8080
   MESHCENTRAL_SERVER_URL=https://<subdominio>:8083
   ```
   `ARCHIVOS_BASE_URL` es la dirección de descarga que va dentro de cada orden: mientras
   haya estaciones que no resuelven el subdominio, conviene dejarla con la IP.
5. `sh desplegar.sh` y republicar los catálogos.
6. Distribuir la orden "cambiar servidor" por tandas (como el agente), empezando por las
   estaciones de prueba.
7. Mantener la IP en el certificado y en `ALLOWED_HOSTS` hasta que **todas** las
   estaciones reporten el subdominio.

---

## Lista de control

- [ ] Todas las variables de `deploy/.env` están en KeePass y coinciden con el NUC.
- [ ] `cert.pem` tiene `IP Address:10.111.6.20` en el SAN.
- [ ] Ensayo completo en el servidor nuevo, con el respaldo del día anterior.
- [ ] Respaldo final: base, media, EMQX y MeshCentral, con SHA-256 verificado.
- [ ] Base restaurada, sin migraciones pendientes.
- [ ] EMQX importado: aparecen los usuarios de las estaciones.
- [ ] Media y MeshCentral restaurados.
- [ ] NUC apagado antes de asignar la IP al servidor nuevo.
- [ ] Estaciones en línea, MeshCentral conecta, una orden de prueba llega.
- [ ] Catálogos republicados.
- [ ] Timers de respaldo y salud activos en el servidor nuevo.
- [ ] NUC guardado intacto para volver atrás.
