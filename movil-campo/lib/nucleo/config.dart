import 'package:shared_preferences/shared_preferences.dart';

/// Servidor al que apunta la app. Es configurable porque el mismo APK se instala
/// contra distintos entornos (produccion y pruebas) sin recompilar.
class Config {
  static const _ipPorDefecto = '10.111.6.20';
  static const _puertoPorDefecto = 8084;

  static String _ip = _ipPorDefecto;
  static int _puerto = _puertoPorDefecto;
  static bool _configurado = false;

  static String get ip => _ip;
  static int get puerto => _puerto;
  static bool get configurado => _configurado;

  /// Siempre HTTPS: el servidor no expone la API sin cifrar, y aceptar HTTP
  /// habilitaria una degradacion en la red de una farmacia.
  static String get urlBase => 'https://$_ip:$_puerto/api/v1';

  static Future<void> cargar() async {
    final prefs = await SharedPreferences.getInstance();
    _ip = prefs.getString('servidor_ip') ?? _ipPorDefecto;
    _puerto = prefs.getInt('servidor_puerto') ?? _puertoPorDefecto;
    _configurado = prefs.getBool('servidor_configurado') ?? false;
  }

  static Future<void> guardar(String ip, int puerto) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString('servidor_ip', ip);
    await prefs.setInt('servidor_puerto', puerto);
    await prefs.setBool('servidor_configurado', true);
    _ip = ip;
    _puerto = puerto;
    _configurado = true;
  }
}
