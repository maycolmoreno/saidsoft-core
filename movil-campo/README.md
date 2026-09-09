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

## Compilar el release firmado

El APK que se le instala a un técnico va firmado con el keystore de CRESIO
(`android/cresio-campo-release.jks`, alias `cresio-campo`, RSA 2048, válido hasta
2054). Ni el keystore ni `android/key.properties` están en git — ver
`android/key.properties.example` para el formato.

```bash
cd movil-campo
flutter build apk --release --split-per-abi
# build/app/outputs/flutter-apk/app-arm64-v8a-release.apk  <- el de casi cualquier
#                                                             teléfono actual
```

`--split-per-abi` no es opcional en la práctica: el APK universal empaqueta el runtime
de Flutter para las tres arquitecturas y multiplica el tamaño de la descarga por tres,
que es lo que dolía en el APK de depuración de ~154 MB publicado a mano.

**La firma es autofirmada y no pasa por Play Store**: la app se distribuye por fuera, y
el teléfono va a pedir permiso para instalar de un origen desconocido la primera vez.
Para verificar que un APK en circulación es el nuestro y no otro, su huella tiene que
dar:

```
SHA256: 73:9A:43:B7:70:BE:7C:86:87:77:EA:5D:2A:7F:C1:C7:29:1F:7E:C0:7D:60:60:5B:33:09:E1:F3:E0:7F:0A:9C
```

### Por qué el keystore importa más que el código

Android identifica una app por `applicationId` **+ firma**. Dos consecuencias que no se
arreglan con un parche:

- **Si se pierde el keystore o su contraseña**, no hay forma de publicar una
  actualización que los teléfonos ya instalados acepten: hay que desinstalar y volver a
  instalar en cada equipo, perdiendo los datos locales de la app (cola offline incluida).
  Tiene que existir una copia fuera de esta máquina.
- **Si se filtra**, cualquiera puede firmar un APK que los teléfonos van a instalar
  encima del nuestro como si fuera una actualización legítima — con el certificado del
  servidor empaquetado adentro.

Sin `key.properties`, un build de release **falla con un mensaje explícito** en vez de
caer en las llaves de depuración. Eso era el comportamiento anterior (el `TODO` que dejó
la plantilla de Flutter) y es peor que un error: produce un APK que parece publicable,
Android lo trata como una app distinta de la firmada, y no puede actualizarla. Los builds
de depuración siguen funcionando sin el keystore.
