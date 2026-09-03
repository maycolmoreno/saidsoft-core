import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:connectivity_plus/connectivity_plus.dart';
import 'package:http/http.dart' as http;

import '../config/app_config.dart';
import 'http_seguro.dart';
import '../errors/exceptions.dart';
import '../storage/secure_storage_service.dart';

typedef UnauthorizedHandler = Future<void> Function();

class ApiClient {
  ApiClient({
    http.Client? client,
    required SecureStorageService secureStorage,
    required UnauthorizedHandler onUnauthorized,
  })  : _clienteInyectado = client,
        _secureStorage = secureStorage,
        _onUnauthorized = onUnauthorized;

  /// Solo lo setean los tests; en producción se resuelve por HttpSeguro, que es
  /// asíncrono (lee el certificado de los assets) y por eso no puede construirse
  /// en el constructor.
  final http.Client? _clienteInyectado;

  Future<http.Client> get _cliente async =>
      _clienteInyectado ?? await HttpSeguro.cliente();
  final SecureStorageService _secureStorage;
  final UnauthorizedHandler _onUnauthorized;

  static const _timeout = Duration(seconds: 15);

  Future<dynamic> get(String path) => _send('GET', path);

  Future<dynamic> post(String path, dynamic body) =>
      _send('POST', path, body: body);

  Future<dynamic> put(String path, dynamic body) =>
      _send('PUT', path, body: body);

  Future<dynamic> delete(String path) => _send('DELETE', path);

  /// [campoArchivo] es el nombre del campo del formulario. DRF espera uno concreto
  /// por endpoint (ej. "archivo" al adjuntar una imagen); la API anterior recibía
  /// todo bajo "files", que sigue siendo el valor por defecto.
  Future<dynamic> postMultipart(
    String path,
    List<File> files,
    Map<String, String> fields, {
    String campoArchivo = 'files',
  }) async {
    await _ensureConnectivity();
    final uri = Uri.parse('${AppConfig.baseUrl}$path');
    final request = http.MultipartRequest('POST', uri);
    request.headers.addAll(await _headers(includeJson: false));
    request.fields.addAll(fields);
    for (final file in files) {
      request.files
          .add(await http.MultipartFile.fromPath(campoArchivo, file.path));
    }

    try {
      // request.send() crearía un Client propio, sin el certificado de SAIDSOFT
      // confiado -- la subida fallaría con error de TLS aunque el resto de la app
      // funcione. Se envía por nuestro cliente.
      final cliente = await _cliente;
      final streamed = await cliente.send(request).timeout(_timeout);
      final response = await http.Response.fromStream(streamed);
      return _parseResponse(response);
    } on SocketException {
      throw const OfflineException();
    } on TimeoutException {
      throw const OfflineException('No fue posible conectar con el servidor.');
    }
  }

  Future<dynamic> _send(
    String method,
    String path, {
    dynamic body,
  }) async {
    await _ensureConnectivity();
    final uri = Uri.parse('${AppConfig.baseUrl}$path');
    var headers = await _headers();
    final cliente = await _cliente;

    try {
      late http.Response response;
      switch (method) {
        case 'POST':
          response = await cliente
              .post(uri, headers: headers, body: jsonEncode(body ?? {}))
              .timeout(_timeout);
          break;
        case 'PUT':
          response = await cliente
              .put(uri, headers: headers, body: jsonEncode(body ?? {}))
              .timeout(_timeout);
          break;
        case 'DELETE':
          response =
              await cliente.delete(uri, headers: headers).timeout(_timeout);
          break;
        default:
          response = await cliente.get(uri, headers: headers).timeout(_timeout);
      }

      return _parseResponse(response);
    } on SocketException {
      throw const OfflineException();
    } on TimeoutException {
      throw const OfflineException('Tiempo de espera agotado.');
    }
  }

  Future<void> _ensureConnectivity() async {
    final results = await Connectivity().checkConnectivity();
    if (results.contains(ConnectivityResult.none)) {
      throw const OfflineException();
    }
  }

  Future<Map<String, String>> _headers({bool includeJson = true}) async {
    final token = await _secureStorage.readToken();
    return {
      'Accept': 'application/json',
      if (includeJson) 'Content-Type': 'application/json',
      // "Token", no "Basic": el backend Django usa TokenAuthentication de DRF
      // (config/settings/base.py). El token se obtiene una vez en el login y ya
      // no se guardan las credenciales del técnico en el dispositivo.
      if (token != null && token.isNotEmpty) 'Authorization': 'Token $token',
    };
  }

  dynamic _parseResponse(http.Response response) {
    if (response.statusCode >= 200 && response.statusCode < 300) {
      if (response.body.isEmpty) {
        return null;
      }
      return jsonDecode(response.body);
    }

    if (response.statusCode == 401) {
      _secureStorage.clearSession();
      unawaited(_onUnauthorized());
      throw const AuthException('Sesion expirada. Inicia sesion nuevamente.');
    }
    if (response.statusCode == 403) {
      throw const AuthException('Sin permisos.');
    }
    if (response.statusCode == 404) {
      throw const NotFoundException();
    }
    if (response.statusCode >= 500) {
      throw const ServerException();
    }
    throw ServerException(_friendlyMessage(response.body));
  }

  String _friendlyMessage(String body) {
    if (body.isEmpty) {
      return 'No fue posible completar la operacion.';
    }
    try {
      final json = jsonDecode(body);
      return json['message']?.toString() ??
          json['error']?.toString() ??
          'No fue posible completar la operacion.';
    } catch (_) {
      return 'No fue posible completar la operacion.';
    }
  }
}
