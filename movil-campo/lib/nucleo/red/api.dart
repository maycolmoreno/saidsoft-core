import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:connectivity_plus/connectivity_plus.dart';
import 'package:http/http.dart' as http;

import '../almacen/almacen_seguro.dart';
import '../config.dart';
import 'http_seguro.dart';

/// Errores que la interfaz necesita distinguir para decirle al técnico qué hacer.
/// Un único "no se pudo conectar" para todo obliga a adivinar, que es exactamente
/// lo que hay que evitar en campo.
sealed class ErrorApi implements Exception {
  const ErrorApi(this.mensaje);
  final String mensaje;
  @override
  String toString() => mensaje;
}

/// Sin red. Es esperable en una farmacia: NO es un fallo, dispara la cola offline.
class SinConexion extends ErrorApi {
  const SinConexion([super.mensaje = 'Sin conexion con el servidor.']);
}

/// El certificado del servidor no es el que trae la app (se regeneró).
class CertificadoInvalido extends ErrorApi {
  const CertificadoInvalido([
    super.mensaje = 'El certificado del servidor no coincide con el de la app. '
        'Hace falta una version actualizada.',
  ]);
}

/// Token vencido o revocado: hay que volver a entrar.
class SesionExpirada extends ErrorApi {
  const SesionExpirada([super.mensaje = 'Tu sesion expiro. Volve a iniciar sesion.']);
}

class SinPermiso extends ErrorApi {
  const SinPermiso([super.mensaje = 'No tenes permiso para esta accion.']);
}

class NoEncontrado extends ErrorApi {
  const NoEncontrado([super.mensaje = 'El servidor no tiene ese recurso.']);
}

/// El servidor rechazó los datos (400). `detalle` trae lo que dijo, que suele ser
/// accionable ("Ya hay un mantenimiento abierto para CR-DSK-0001").
class DatosRechazados extends ErrorApi {
  const DatosRechazados(super.mensaje);
}

class ErrorServidor extends ErrorApi {
  const ErrorServidor([super.mensaje = 'El servidor tuvo un problema.']);
}

typedef AlExpirarSesion = Future<void> Function();

/// Cliente HTTP de la API. Todo pasa por acá: una sola definición del transporte,
/// del token y de la traducción de errores.
class Api {
  Api({
    required AlmacenSeguro almacen,
    AlExpirarSesion? alExpirarSesion,
    http.Client? clienteParaTests,
  })  : _almacen = almacen,
        _alExpirarSesion = alExpirarSesion,
        _clienteInyectado = clienteParaTests;

  final AlmacenSeguro _almacen;
  final AlExpirarSesion? _alExpirarSesion;
  final http.Client? _clienteInyectado;

  static const _espera = Duration(seconds: 20);

  Future<http.Client> get _cliente async =>
      _clienteInyectado ?? await HttpSeguro.cliente();

  Future<dynamic> obtener(String ruta) => _enviar('GET', ruta);

  Future<dynamic> publicar(String ruta, [Object? cuerpo]) =>
      _enviar('POST', ruta, cuerpo: cuerpo);

  /// Sube UN archivo. El backend recibe un archivo por llamada bajo el nombre de
  /// campo que indica cada endpoint.
  Future<dynamic> subirArchivo(
    String ruta,
    File archivo, {
    String campo = 'archivo',
  }) async {
    await _exigirConexion();
    final peticion = http.MultipartRequest('POST', Uri.parse('${Config.urlBase}$ruta'))
      ..headers.addAll(await _cabeceras(conJson: false))
      ..files.add(await http.MultipartFile.fromPath(campo, archivo.path));

    try {
      // Se envía por NUESTRO cliente: peticion.send() crearía uno propio, sin el
      // certificado confiado, y la subida fallaría con error de TLS.
      final cliente = await _cliente;
      final flujo = await cliente.send(peticion).timeout(_espera);
      return _interpretar(await http.Response.fromStream(flujo));
    } on SocketException {
      throw const SinConexion();
    } on HandshakeException {
      throw const CertificadoInvalido();
    } on TimeoutException {
      throw const SinConexion('El servidor no respondio a tiempo.');
    }
  }

  Future<dynamic> _enviar(String metodo, String ruta, {Object? cuerpo}) async {
    await _exigirConexion();
    final uri = Uri.parse('${Config.urlBase}$ruta');
    final cabeceras = await _cabeceras();
    final cliente = await _cliente;

    try {
      final respuesta = switch (metodo) {
        'POST' => await cliente
            .post(uri, headers: cabeceras, body: jsonEncode(cuerpo ?? {}))
            .timeout(_espera),
        _ => await cliente.get(uri, headers: cabeceras).timeout(_espera),
      };
      return _interpretar(respuesta);
    } on SocketException {
      throw const SinConexion();
    } on HandshakeException {
      throw const CertificadoInvalido();
    } on TimeoutException {
      throw const SinConexion('El servidor no respondio a tiempo.');
    } on http.ClientException {
      throw const SinConexion();
    }
  }

  /// Corta antes de intentar la llamada cuando no hay red, para que la cola offline
  /// se dispare de inmediato en vez de esperar el timeout completo.
  Future<void> _exigirConexion() async {
    final estado = await Connectivity().checkConnectivity();
    if (estado.contains(ConnectivityResult.none)) {
      throw const SinConexion();
    }
  }

  Future<Map<String, String>> _cabeceras({bool conJson = true}) async {
    final token = await _almacen.leerToken();
    return {
      'Accept': 'application/json',
      if (conJson) 'Content-Type': 'application/json',
      // "Token": el backend usa TokenAuthentication de DRF.
      if (token != null && token.isNotEmpty) 'Authorization': 'Token $token',
    };
  }

  dynamic _interpretar(http.Response respuesta) {
    final codigo = respuesta.statusCode;
    if (codigo >= 200 && codigo < 300) {
      if (respuesta.body.isEmpty) return null;
      return jsonDecode(utf8.decode(respuesta.bodyBytes));
    }
    if (codigo == 401) {
      _almacen.borrarSesion();
      final alExpirar = _alExpirarSesion;
      if (alExpirar != null) unawaited(alExpirar());
      throw const SesionExpirada();
    }
    if (codigo == 403) throw const SinPermiso();
    if (codigo == 404) throw const NoEncontrado();
    if (codigo >= 500) throw const ErrorServidor();
    throw DatosRechazados(_mensajeDe(respuesta));
  }

  /// Extrae algo legible del cuerpo de error de DRF, que puede ser
  /// {"detail": "..."} o {"campo": ["error", ...]}.
  String _mensajeDe(http.Response respuesta) {
    try {
      final cuerpo = jsonDecode(utf8.decode(respuesta.bodyBytes));
      if (cuerpo is Map) {
        if (cuerpo['detail'] != null) return cuerpo['detail'].toString();
        final partes = <String>[];
        cuerpo.forEach((campo, valor) {
          final texto = valor is List ? valor.join(' ') : valor.toString();
          partes.add('$campo: $texto');
        });
        if (partes.isNotEmpty) return partes.join('\n');
      }
    } catch (_) {
      // Cuerpo no-JSON: se cae al mensaje genérico de abajo.
    }
    return 'El servidor rechazo los datos (${respuesta.statusCode}).';
  }
}
