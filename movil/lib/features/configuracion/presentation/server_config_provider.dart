import 'dart:async';
import 'dart:io';

import 'package:flutter/foundation.dart';

import '../../../core/config/app_config.dart';
import '../../../core/network/http_seguro.dart';

class ServerConfigProvider extends ChangeNotifier {
  String? _message;
  bool _success = false;
  bool _loading = false;

  String? get message => _message;
  bool get success => _success;
  bool get loading => _loading;

  /// Comprueba que del otro lado haya un servidor SAIDSOFT alcanzable.
  ///
  /// Se prueba contra `/auth/yo/` SIN token: un 401 es una respuesta VÁLIDA acá --
  /// significa que el servidor está, habla nuestra API y pide credenciales, que es
  /// justo lo que queremos saber antes de que el técnico intente el login. Un 404
  /// significaría que la IP responde pero no es SAIDSOFT.
  ///
  /// Cada modo de falla devuelve un mensaje distinto: antes cualquier problema
  /// (certificado, red, servidor equivocado) mostraba "No se pudo conectar" a secas
  /// y no había forma de saber qué revisar.
  Future<bool> testConnection(String ip, int port) async {
    _loading = true;
    _message = null;
    notifyListeners();

    try {
      // HTTPS y el cliente con el certificado propio confiado: con http.get() a
      // secas la prueba fallaba siempre, aunque el servidor estuviera bien.
      final cliente = await HttpSeguro.cliente();
      final url = Uri.parse('https://$ip:$port/api/v1/auth/yo/');
      final response =
          await cliente.get(url).timeout(const Duration(seconds: 10));

      if (response.statusCode == 401 || response.statusCode == 200) {
        _success = true;
        _message = 'Servidor conectado';
      } else if (response.statusCode == 404) {
        _success = false;
        _message =
            'Responde, pero no es un servidor SAIDSOFT (revisa la IP y el puerto).';
      } else {
        _success = false;
        _message = 'El servidor respondio ${response.statusCode}.';
      }
      return _success;
    } on HandshakeException {
      _success = false;
      _message = 'El certificado del servidor no coincide con el de la app. '
          'Puede que se haya regenerado: hace falta una version nueva de la app.';
      return false;
    } on TimeoutException {
      _success = false;
      _message = 'El servidor no respondio a tiempo. Revisa que estes en la red interna.';
      return false;
    } on SocketException catch (e) {
      _success = false;
      _message = 'No se pudo alcanzar $ip:$port (${e.osError?.message ?? 'sin ruta'}). '
          'Revisa la IP, el puerto y que el telefono este en la red interna.';
      return false;
    } catch (e) {
      _success = false;
      _message = 'No se pudo conectar: $e';
      return false;
    } finally {
      _loading = false;
      notifyListeners();
    }
  }

  Future<void> save(String ip, int port) async {
    await AppConfig.save(ip, port);
    _success = true;
    _message = 'Configuracion guardada';
    notifyListeners();
  }
}
