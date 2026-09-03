# SAIDSOFT Campo

App de los técnicos en campo. Habla con la API REST de SAIDSOFT (`/api/v1/`, Django
REST Framework).

Reemplaza a `movil/` (portada de InvTICS), que arrastraba el modelo de datos de un
sistema distinto: custodios/custodias, códigos SAP y una tabla de ubicaciones que en
producción está vacía.

## Alcance

Solo lo que un técnico hace en campo:

- **Trabajo**: sus mantenimientos, ordenados por urgencia real (SLA, no fecha).
- **Detalle**: registrar llegada, checklist, fotos, firma y cierre con resultado.
- **Visitas**: sus visitas técnicas, llegada y cierre.
- **Ubicación**: envío de posición con consentimiento explícito.

Queda deliberadamente afuera: catálogo de equipos navegable, planificación,
notificaciones push y mapa en tiempo real. Se agregan cuando alguien los pida.

## Decisiones que conviene no revertir sin pensarlas

**El orden de la lista lo decide el SLA, no la fecha.** Un correctivo crítico de hace
10 minutos va antes que un preventivo agendado la semana pasada. Ordenar por fecha
—lo obvio— entierra justo lo que no puede esperar.

**El certificado se empaqueta y se confía explícitamente** (`assets/certs/cert.pem`),
en vez de desactivar la validación TLS. Apagarla dejaría la app aceptando cualquier
certificado, incluido el de un atacante en la red de una farmacia. Si el servidor
regenera el suyo, hay que actualizar el archivo y publicar una versión nueva.

**Los permisos son los codenames de Django**, los mismos que evalúa el panel web. No
hay una tabla de roles propia de la app que pueda desincronizarse.

**Todo lo que muta estado se encola si no hay red** (`ColaOffline`) y se sube solo al
recuperar señal. Las lecturas NO: mostrar datos viejos como si fueran de ahora es
peor que decir "sin conexión". Las fotos tampoco se encolan — pesan megabytes y
llenarían la base del teléfono.

**El catálogo de resultados de cierre vive en la app**, no se pide por API: si se
pidiera, cerrar un mantenimiento en una farmacia sin señal sería imposible. Debe
seguir a `ResultadoTecnico` del backend.

**El GPS solo corre con la pantalla abierta.** Se evita
`ACCESS_BACKGROUND_LOCATION`, que es un permiso sensible y hay que justificarlo. El
técnico enciende el envío a mano.

## Certificado

```bash
echo | openssl s_client -connect 10.111.6.20:8084 2>/dev/null \
  | openssl x509 > movil-campo/assets/certs/cert.pem
```

## Desarrollo

```bash
cd movil-campo
flutter pub get
flutter analyze
flutter test
flutter build apk --debug
```
