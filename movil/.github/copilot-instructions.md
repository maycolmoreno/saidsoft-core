# crescio_mobile — Flutter Android App (Técnicos CRESIO)

## Arquitectura

**Feature-first** con **Provider** (ChangeNotifier) para state management.

```
lib/
├── core/                # Infraestructura compartida
│   ├── config/          # AppConfig (IP/puerto servidor)
│   ├── network/         # ApiClient (HTTP + JWT)
│   ├── storage/         # SecureStorage, SQLite (LocalDatabase)
│   ├── errors/          # Excepciones personalizadas
│   └── models/          # DTOs compartidos
├── features/            # Módulos por funcionalidad
│   ├── auth/            # Login/logout
│   ├── equipos/         # Gestión de equipos
│   ├── mantenimientos/  # Órdenes + checklist + fotos + firmas
│   ├── visitas/         # Visitas técnicas
│   ├── gps/             # Seguimiento ubicación
│   ├── planificacion/   # Tareas programadas
│   ├── notificaciones/  # Push notifications locales
│   ├── sync/            # Sincronización offline-first
│   ├── dashboard/       # Shell con navegación inferior
│   └── configuracion/   # Configuración de servidor
├── shared/              # Tema visual, widgets reutilizables
└── main.dart            # Entry point con MultiProvider
```

### Estructura por feature

```
feature/
├── data/           # Repositorios, modelos de datos
├── domain/         # Casos de uso (cuando aplica)
└── presentation/   # Providers, Screens, Widgets
```

## Build y ejecución

```powershell
# Requiere Flutter 3.x en PATH
flutter pub get
flutter run
flutter build apk --release
```

> **Nota**: Flutter no está instalado en este workspace. Ver README.md para setup.

## Stack técnico

- **Flutter SDK** con **Dart**
- **Provider 6.1.2** — state management
- **http 1.2.0** — cliente HTTP
- **sqflite 2.3.3** — SQLite para datos offline
- **flutter_secure_storage 9.2.2** — almacenamiento seguro de JWT
- **mobile_scanner 5.2.3** — escaneo QR/barcodes
- **signature 5.5.0** — captura de firmas digitales
- **geolocator 13.0.2** — GPS
- **connectivity_plus 6.0.5** — detección de conectividad
- **image_picker 1.1.2** — selección de imágenes

## Conexión con backend

- URL dinámica: `http://{serverIp}:{serverPort}/api` (configurable en runtime)
- Defaults: `0.0.0.0:8083`
- Autenticación: `Authorization: Basic $token` (JWT almacenado en SecureStorage)
- Timeout: 15 segundos
- Callback `onUnauthorized()` para logout automático
- Manejo de `OfflineException` cuando no hay conectividad

## Almacenamiento local

- **SecureStorage**: JWT, username, displayName, role, userId
- **SQLite** (LocalDatabase): mantenimientos_pendientes, fotos_pendientes, firmas_pendientes, sync_queue
- **SharedPreferences**: IP/puerto del servidor, flag de configuración

## Flujo de autenticación

```
Loading → RequiresServerConfig → Login → Authenticated → Dashboard
```

## Configuración Android

- Application ID: `com.cresio.mobile`
- Java/Kotlin JVM target: **17**
- Core Library Desugaring habilitado
- Firma release: pendiente de configurar (ver `key.properties`)

## Convenciones

- Idioma del código: **español** (nombres de clases, variables)
- Widgets siguen Material Design
- Tema corporativo definido en `shared/theme/`
- Offline-first: datos se cachean en SQLite y se sincronizan cuando hay conexión
