import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;

import '../../../core/config/app_config.dart';

class ServerConfigProvider extends ChangeNotifier {
  String? _message;
  bool _success = false;
  bool _loading = false;

  String? get message => _message;
  bool get success => _success;
  bool get loading => _loading;

  Future<bool> testConnection(String ip, int port) async {
    _loading = true;
    _message = null;
    notifyListeners();

    try {
      final url = Uri.parse('http://$ip:$port/api/setup/necesario');
      final response = await http.get(url).timeout(const Duration(seconds: 10));
      _success = response.statusCode == 200;
      _message = _success
          ? 'Servidor conectado'
          : 'No se pudo conectar';
      return _success;
    } catch (_) {
      _success = false;
      _message = 'No se pudo conectar';
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
