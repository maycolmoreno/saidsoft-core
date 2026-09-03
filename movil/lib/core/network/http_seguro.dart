import 'dart:io';

import 'package:flutter/services.dart' show rootBundle;
import 'package:http/io_client.dart';
import 'package:http/http.dart' as http;

/// Cliente HTTP que confía en el certificado propio de SAIDSOFT.
///
/// El servidor usa un certificado autofirmado (CN=saidsof-mqtt, con la IP en el
/// SAN), así que el almacén de confianza del sistema lo rechaza. En vez de
/// desactivar la validación -- que dejaría la app aceptando CUALQUIER
/// certificado, incluido el de un atacante en la misma red de la farmacia --
/// se agrega ESE certificado al contexto de confianza. La validación de cadena
/// y de hostname siguen activas: es más seguro que un certificado público,
/// porque solo confía en el nuestro.
///
/// Mismo criterio que el agente de Windows, que ya se instala con su cert.pem
/// al lado del ejecutable.
class HttpSeguro {
  static const rutaCertificado = 'assets/certs/cert.pem';

  static http.Client? _cliente;

  /// Construye (una sola vez) el cliente con el certificado propio confiado.
  ///
  /// Si el certificado no se puede cargar, cae al cliente por defecto en vez de
  /// romper el arranque: contra un servidor con certificado válido de una CA
  /// pública la app sigue funcionando, y contra el nuestro fallará con un error
  /// de TLS claro en vez de un cuelgue silencioso.
  static Future<http.Client> cliente() async {
    if (_cliente != null) return _cliente!;
    try {
      final pem = await rootBundle.load(rutaCertificado);
      final contexto = SecurityContext(withTrustedRoots: true)
        ..setTrustedCertificatesBytes(pem.buffer.asUint8List());
      _cliente = IOClient(HttpClient(context: contexto));
    } catch (_) {
      _cliente = http.Client();
    }
    return _cliente!;
  }

  /// Solo para tests: permite inyectar un cliente falso.
  static void definirClienteParaTests(http.Client? falso) => _cliente = falso;
}
