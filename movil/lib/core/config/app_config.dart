import 'package:shared_preferences/shared_preferences.dart';

class AppConfig {
  static const _defaultServerIp = '10.111.6.20';
  static const _defaultServerPort = 8084;
  static String _serverIp = _defaultServerIp;
  static int _serverPort = _defaultServerPort;
  static bool _configured = false;

  static String get serverIp => _serverIp;
  static int get serverPort => _serverPort;
  static bool get isConfigured => _configured;

  /// HTTPS y /api/v1: el backend Django expone la API versionada bajo ese
  /// prefijo (config/urls.py) y nginx solo sirve TLS en 8084. El certificado es
  /// propio, ver HttpSeguro -- no se acepta cualquier certificado.
  static String get baseUrl => 'https://$_serverIp:$_serverPort/api/v1';

  static Future<void> load() async {
    final prefs = await SharedPreferences.getInstance();
    _serverIp = prefs.getString('server_ip') ?? _defaultServerIp;
    _serverPort = prefs.getInt('server_port') ?? _defaultServerPort;
    _configured = prefs.getBool('server_configured') ?? false;
  }

  static Future<void> save(String ip, int port) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString('server_ip', ip);
    await prefs.setInt('server_port', port);
    await prefs.setBool('server_configured', true);
    _serverIp = ip;
    _serverPort = port;
    _configured = true;
  }
}
