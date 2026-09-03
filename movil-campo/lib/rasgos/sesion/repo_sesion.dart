import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:http/http.dart' as http;

import '../../nucleo/almacen/almacen_seguro.dart';
import '../../nucleo/config.dart';
import '../../nucleo/red/api.dart';
import '../../nucleo/red/http_seguro.dart';
import 'sesion.dart';

class RepoSesion {
  RepoSesion({required AlmacenSeguro almacen, required Api api})
      : _almacen = almacen,
        _api = api;

  final AlmacenSeguro _almacen;
  final Api _api;

  /// Login en dos pasos: `/auth/token/` canjea usuario+clave por un token y
  /// `/auth/yo/` trae identidad y permisos. Son dos llamadas porque
  /// `obtain_auth_token` de DRF devuelve solo el token.
  ///
  /// No se usa el cliente `Api` para el primer paso porque todavía no hay token que
  /// poner en la cabecera.
  Future<Sesion> iniciarSesion(String usuario, String clave) async {
    final cliente = await HttpSeguro.cliente();
    late final http.Response respuesta;
    try {
      respuesta = await cliente
          .post(
            Uri.parse('${Config.urlBase}/auth/token/'),
            headers: const {
              'Accept': 'application/json',
              'Content-Type': 'application/json',
            },
            body: jsonEncode({'username': usuario, 'password': clave}),
          )
          .timeout(const Duration(seconds: 20));
    } on SocketException {
      throw const SinConexion();
    } on HandshakeException {
      throw const CertificadoInvalido();
    } on TimeoutException {
      throw const SinConexion('El servidor no respondio a tiempo.');
    } on http.ClientException {
      throw const SinConexion();
    }

    // DRF responde 400 con {"non_field_errors": [...]} cuando las credenciales no
    // sirven; para el técnico eso es simplemente "usuario o clave incorrectos".
    if (respuesta.statusCode == 400 || respuesta.statusCode == 401) {
      throw const DatosRechazados('Usuario o clave incorrectos.');
    }
    if (respuesta.statusCode < 200 || respuesta.statusCode >= 300) {
      throw const ErrorServidor('No fue posible iniciar sesion.');
    }

    final token =
        (jsonDecode(respuesta.body) as Map<String, dynamic>)['token']?.toString();
    if (token == null || token.isEmpty) {
      throw const ErrorServidor('El servidor no devolvio un token.');
    }

    // Se guarda primero para que la llamada siguiente ya lleve la cabecera.
    await _almacen.guardarSesion(
      token: token, usuario: usuario, nombre: usuario, id: null,
      permisos: const [], esStaff: false,
    );
    return _leerYo(token);
  }

  /// Relee identidad y permisos del token guardado. Se llama al arrancar para que un
  /// cambio de permisos en el panel se refleje sin obligar a cerrar sesión.
  Future<Sesion?> recuperarSesion() async {
    final token = await _almacen.leerToken();
    if (token == null || token.isEmpty) return null;
    try {
      return await _leerYo(token);
    } on SinConexion {
      // Sin red se sigue con lo guardado: el técnico tiene que poder abrir la app
      // dentro de una farmacia sin señal.
      return Sesion(
        token: token,
        usuario: await _almacen.leerUsuario() ?? '',
        nombre: await _almacen.leerNombre() ?? '',
        id: await _almacen.leerId(),
        permisos: await _almacen.leerPermisos(),
        esStaff: await _almacen.leerEsStaff(),
      );
    } on SesionExpirada {
      return null;
    }
  }

  Future<Sesion> _leerYo(String token) async {
    final datos = await _api.obtener('/auth/yo/') as Map<String, dynamic>;
    final sesion = Sesion.desdeJson(token, datos);
    await _almacen.guardarSesion(
      token: sesion.token,
      usuario: sesion.usuario,
      nombre: sesion.nombre,
      id: sesion.id,
      permisos: sesion.permisos,
      esStaff: sesion.esStaff,
    );
    return sesion;
  }

  Future<void> cerrarSesion() => _almacen.borrarSesion();
}
