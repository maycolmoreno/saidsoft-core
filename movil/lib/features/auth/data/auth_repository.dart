import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:http/http.dart' as http;

import '../../../core/config/app_config.dart';
import '../../../core/network/http_seguro.dart';
import '../../../core/errors/exceptions.dart';
import '../../../core/storage/secure_storage_service.dart';
import 'auth_models.dart';

class AuthRepository {
  AuthRepository({
    required SecureStorageService secureStorage,
  }) : _secureStorage = secureStorage;

  final SecureStorageService _secureStorage;

  /// Login contra DRF: `POST /auth/token/` canjea usuario+clave por un token, y
  /// `GET /auth/yo/` trae identidad y permisos. Son dos llamadas porque
  /// `obtain_auth_token` devuelve solo el token.
  ///
  /// A diferencia del esquema Basic anterior, la clave del técnico NO queda
  /// guardada en el dispositivo: solo el token, que el servidor puede revocar.
  Future<AuthSession> login(LoginRequest request) async {
    final cliente = await HttpSeguro.cliente();
    late final http.Response respuestaToken;
    try {
      respuestaToken = await cliente
          .post(
            Uri.parse('${AppConfig.baseUrl}/auth/token/'),
            headers: {
              'Accept': 'application/json',
              'Content-Type': 'application/json',
            },
            body: jsonEncode(request.toJson()),
          )
          .timeout(const Duration(seconds: 15));
    } on TimeoutException {
      throw const AuthException(
        'No fue posible validar el usuario. Verifica tus credenciales y la conexion con el servidor.',
      );
    } on SocketException {
      throw const OfflineException(
        'No fue posible conectar con el servidor. Revisa la URL configurada y tu conexion.',
      );
    } on HandshakeException {
      throw const OfflineException(
        'El servidor presento un certificado que la app no reconoce. '
        'Revisa que la app este actualizada con el certificado vigente.',
      );
    } on http.ClientException {
      throw const OfflineException(
        'No fue posible conectar con el servidor. Revisa la URL configurada y tu conexion.',
      );
    }

    if (respuestaToken.statusCode == 400 || respuestaToken.statusCode == 401) {
      throw const AuthException('Credenciales incorrectas.');
    }
    if (respuestaToken.statusCode < 200 || respuestaToken.statusCode >= 300) {
      throw const AuthException('No fue posible iniciar sesion.');
    }

    final token = (jsonDecode(respuestaToken.body)
        as Map<String, dynamic>)['token']?.toString();
    if (token == null || token.isEmpty) {
      throw const AuthException('El servidor no devolvio un token de sesion.');
    }

    final sesion = await _leerUsuarioActual(token, request.username);
    await _persistSession(sesion);
    return sesion;
  }

  /// Identidad + permisos del usuario del token. Compartido por login y refresh.
  Future<AuthSession> _leerUsuarioActual(String token, String usernamePorDefecto) async {
    final cliente = await HttpSeguro.cliente();
    late final http.Response respuesta;
    try {
      respuesta = await cliente.get(
        Uri.parse('${AppConfig.baseUrl}/auth/yo/'),
        headers: {
          'Accept': 'application/json',
          'Authorization': 'Token $token',
        },
      ).timeout(const Duration(seconds: 15));
    } on TimeoutException {
      throw const AuthException('No fue posible obtener los datos del usuario.');
    } on SocketException {
      throw const OfflineException(
        'No fue posible conectar con el servidor. Revisa tu conexion.',
      );
    } on http.ClientException {
      throw const OfflineException(
        'No fue posible conectar con el servidor. Revisa tu conexion.',
      );
    }

    if (respuesta.statusCode == 401) {
      throw const AuthException('Tu sesion ha expirado.');
    }
    if (respuesta.statusCode < 200 || respuesta.statusCode >= 300) {
      throw const AuthException('No fue posible obtener los datos del usuario.');
    }

    final datos = jsonDecode(respuesta.body) as Map<String, dynamic>;
    final permisos = _parseModules(datos['permisos']);
    return AuthSession(
      token: token,
      username: datos['username']?.toString() ?? usernamePorDefecto,
      displayName: datos['nombre']?.toString() ?? usernamePorDefecto,
      // Django no tiene un "rol" unico: la app decide por permisos (ver
      // AuthSession._permisoACapacidad). El rol solo alimenta las etiquetas y el
      // respaldo por rol; "admin"/"tecnico" son los valores que userRole reconoce
      // -- "staff" caia en UserRole.unknown y dejaba la sesion sin capacidades.
      role: (datos['es_staff'] == true) ? 'admin' : 'tecnico',
      userId: datos['id'] is int
          ? datos['id'] as int
          : int.tryParse(datos['id']?.toString() ?? ''),
      // `modules` pasa a llevar los codenames de permiso de Django: son los
      // mismos que evalua el panel, asi la app y la web habilitan lo mismo.
      modules: permisos,
      modulesLoaded: datos.containsKey('permisos'),
    );
  }

  Future<void> logout() => _secureStorage.clearSession();

  Future<AuthSession?> readStoredSession() async {
    final token = await _secureStorage.readToken();
    if (token == null || token.isEmpty) {
      return null;
    }
    final username = await _secureStorage.readUsername() ?? '';
    final displayName = await _secureStorage.readDisplayName() ?? username;
    final role = await _secureStorage.readRole() ?? '';
    final userIdStr = await _secureStorage.readUserId();
    final modules = await _secureStorage.readModules();
    final modulesLoaded = await _secureStorage.readModulesLoaded();
    final userId = userIdStr != null ? int.tryParse(userIdStr) : null;
    return AuthSession(
      token: token,
      username: username,
      displayName: displayName,
      role: role,
      userId: userId,
      modules: modules,
      modulesLoaded: modulesLoaded,
    );
  }

  Future<AuthSession> refreshSession(AuthSession current) async {
    final sesion = await _leerUsuarioActual(current.token, current.username);
    await _persistSession(sesion);
    return sesion;
  }

  Future<void> _persistSession(AuthSession session) {
    return _secureStorage.saveSession(
      token: session.token,
      username: session.username,
      displayName: session.displayName,
      role: session.role,
      userId: session.userId?.toString(),
      modules: session.modules,
      modulesLoaded: session.modulesLoaded,
    );
  }

  List<String> _parseModules(dynamic rawModules) {
    if (rawModules is! List) {
      return const [];
    }
    return rawModules
        .map((item) => item?.toString().trim() ?? '')
        .where((item) => item.isNotEmpty)
        .toList();
  }
}
