import 'red/api.dart';

/// Numero de build de ESTA copia de la app.
///
/// Tiene que coincidir con el `+N` de `version: X.Y.Z+N` en `pubspec.yaml`, que es el
/// `versionCode` de Android: un entero que solo sube. Se compara ese numero y no la
/// version con puntos porque '1.10.0' es MENOR que '1.9.0' comparado como texto.
///
/// Esta escrito a mano en vez de leerse con package_info_plus para no sumar un plugin
/// nativo por un entero; el costo es que puede quedar viejo, y por eso hay una prueba
/// (`version_test.dart`) que lo compara contra pubspec.yaml y falla si no coinciden.
const buildActual = 12;

/// Version publicada en el servidor, o null si todavia no se publico ninguna.
class VersionPublicada {
  const VersionPublicada({
    required this.version,
    required this.build,
    required this.notas,
    required this.url,
  });

  final String version;
  final int build;
  final String notas;
  final String url;

  /// La unica pregunta que importa: ¿esta app quedo vieja?
  bool get hayQueActualizar => build > buildActual;

  static VersionPublicada? desdeJson(dynamic json) {
    if (json is! Map || json['publicada'] != true) return null;
    final build = json['build'];
    return VersionPublicada(
      version: json['version']?.toString() ?? '',
      build: build is int ? build : int.tryParse('$build') ?? 0,
      notas: json['notas']?.toString() ?? '',
      url: json['url']?.toString() ?? '',
    );
  }
}

/// Consulta si hay una version mas nueva publicada.
///
/// NUNCA lanza. Es un aviso de conveniencia, no una funcion del trabajo: que falle la
/// consulta no puede impedirle a un tecnico abrir la app ni cerrar un mantenimiento.
/// Sin senal, con el endpoint caido o con un servidor viejo que no lo tiene, devuelve
/// null y la app sigue como si nada.
Future<VersionPublicada?> consultarVersionPublicada(Api api) async {
  try {
    return VersionPublicada.desdeJson(await api.obtener('/version-app/'));
  } catch (_) {
    return null;
  }
}
