import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';

import '../../../core/network/api_client.dart';

/// Servicio para gestionar push notifications con Firebase Cloud Messaging.
/// Registra el token FCM en el backend y muestra notificaciones locales
/// cuando la app está en foreground.
class PushNotificacionService {
  PushNotificacionService(this._apiClient);

  final ApiClient _apiClient;
  final FirebaseMessaging _messaging = FirebaseMessaging.instance;
  final FlutterLocalNotificationsPlugin _localNotifications =
      FlutterLocalNotificationsPlugin();

  static const _androidChannel = AndroidNotificationChannel(
    'cresio_notificaciones',
    'Notificaciones CRESIO',
    description: 'Notificaciones de mantenimiento y avisos del sistema CRESIO',
    importance: Importance.high,
  );

  /// Inicializa FCM: solicita permisos, configura canal Android,
  /// obtiene token y escucha mensajes en foreground.
  Future<void> inicializar() async {
    // Solicitar permisos
    final settings = await _messaging.requestPermission(
      alert: true,
      badge: true,
      sound: true,
    );

    if (settings.authorizationStatus == AuthorizationStatus.denied) {
      return;
    }

    // Configurar canal Android para notificaciones locales
    const androidInit = AndroidInitializationSettings('@mipmap/ic_launcher');
    const initSettings = InitializationSettings(android: androidInit);
    await _localNotifications.initialize(initSettings);

    final androidPlugin =
        _localNotifications.resolvePlatformSpecificImplementation<
            AndroidFlutterLocalNotificationsPlugin>();
    if (androidPlugin != null) {
      await androidPlugin.createNotificationChannel(_androidChannel);
    }

    // Obtener y registrar token
    final token = await _messaging.getToken();
    if (token != null) {
      await _registrarToken(token);
    }

    // Escuchar cambios de token
    _messaging.onTokenRefresh.listen(_registrarToken);

    // Escuchar mensajes en foreground
    FirebaseMessaging.onMessage.listen(_mostrarNotificacionLocal);
  }

  /// Registra el token FCM en el backend.
  Future<void> _registrarToken(String token) async {
    try {
      await _apiClient.post('/notificaciones/fcm-token', {'token': token});
    } catch (_) {
      // Silenciar errores de registro de token (se reintenta en siguiente refresh)
    }
  }

  /// Limpia el token en el backend (al cerrar sesión).
  Future<void> limpiarToken() async {
    try {
      await _apiClient.post('/notificaciones/fcm-token/limpiar', {});
    } catch (_) {
      // Silenciar
    }
  }

  /// Muestra una notificación local cuando se recibe un mensaje en foreground.
  Future<void> _mostrarNotificacionLocal(RemoteMessage message) async {
    final notification = message.notification;
    if (notification == null) return;

    await _localNotifications.show(
      message.hashCode,
      notification.title ?? 'CRESIO',
      notification.body ?? '',
      NotificationDetails(
        android: AndroidNotificationDetails(
          _androidChannel.id,
          _androidChannel.name,
          channelDescription: _androidChannel.description,
          importance: Importance.high,
          priority: Priority.high,
          icon: '@mipmap/ic_launcher',
        ),
      ),
    );
  }
}
