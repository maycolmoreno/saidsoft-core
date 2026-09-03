# cresio_mobile — app móvil de técnicos

App Flutter para los técnicos en campo. Habla con la API REST de SAIDSOFT
(`/api/v1/`, Django REST Framework).

Viene del proyecto InvTICS, donde apuntaba a la API Java/Spring. Se adaptó al
backend Django: autenticación por token, HTTPS con certificado propio y las
rutas/nombres de campo de DRF.

## Estado

Rebanada vertical en curso. **Adaptado y probado:**

- Autenticación por token (`POST /api/v1/auth/token/` + `GET /api/v1/auth/yo/`).
  La clave del técnico ya no se guarda en el dispositivo, solo el token.
- HTTPS contra el certificado propio del servidor, sin desactivar la validación
  TLS (ver `lib/core/network/http_seguro.dart`).
- Los permisos que devuelve `/auth/yo/` son los codenames de Django, los mismos
  que evalúa el panel web.

**Todavía apunta al contrato viejo** (rutas y nombres de campo de Spring): los
repositorios de equipos, notificaciones, ubicaciones, planificación y visitas.
Esas pantallas no funcionan hasta portarlas.

## Certificado

`assets/certs/cert.pem` es el certificado que sirve nginx en producción. La app
confía SOLO en ese: si el servidor lo regenera, hay que actualizar el archivo y
publicar una versión nueva, o los técnicos no van a poder conectarse.

Para actualizarlo:

```bash
echo | openssl s_client -connect 10.111.6.20:8084 2>/dev/null \
  | openssl x509 > movil/assets/certs/cert.pem
```

## Firma de Android

`key.properties` y los `.jks` NO se versionan (traen contraseñas en texto plano;
en el proyecto original venían sin ignorar). Copiar `key.properties.ejemplo` a
`key.properties` y completarlo desde el gestor de secretos del área.

## Desarrollo

```bash
cd movil
flutter pub get
flutter analyze
flutter test
flutter run          # servidor configurable desde la app (Ajustes)
```
