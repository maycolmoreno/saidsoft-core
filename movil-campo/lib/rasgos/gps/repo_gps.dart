import '../../nucleo/almacen/cola_offline.dart';
import '../../nucleo/red/api.dart';

/// Envío de posiciones y consentimiento de monitoreo.
///
/// El backend EXIGE un consentimiento vigente antes de aceptar una posición: no es
/// una formalidad, es el respaldo legal de rastrear a una persona durante su jornada.
class RepoGps {
  const RepoGps(this._api, {ColaOffline? cola}) : _colaInyectada = cola;

  final Api _api;
  final ColaOffline? _colaInyectada;

  ColaOffline get _cola => _colaInyectada ?? ColaOffline.instancia;

  Future<bool> consentimientoRegistrado() async {
    final datos = await _api.obtener('/consentimiento-monitoreo/');
    if (datos is Map) return datos['aceptado'] == true;
    return false;
  }

  Future<void> registrarConsentimiento({required String versionTerminos}) {
    return _api.publicar('/consentimiento-monitoreo/', {
      'aceptado': true,
      'version_terminos': versionTerminos,
    });
  }

  /// Retira el consentimiento. Es un POST con `aceptado: false` y no un DELETE: el
  /// historial es append-only porque hay que poder demostrar que se acepto, cuando y
  /// desde donde -- y retirar el acuerdo es un hecho tan registrable como darlo.
  ///
  /// A partir de acá el backend rechaza las posiciones nuevas: el consentimiento
  /// vigente es el ULTIMO, no "alguno que dijo que si alguna vez".
  Future<void> revocarConsentimiento({required String versionTerminos}) {
    return _api.publicar('/consentimiento-monitoreo/', {
      'aceptado': false,
      'version_terminos': versionTerminos,
    });
  }

  /// Encola si no hay red: la posición de un técnico DENTRO de una farmacia sin
  /// señal es justamente la que hace falta para verificar que estuvo ahí.
  Future<bool> enviarUbicacion({
    required double latitud,
    required double longitud,
    required double? precisionMetros,
    required DateTime capturadaEn,
  }) async {
    final datos = {
      'latitud': latitud,
      'longitud': longitud,
      'precision_metros': precisionMetros,
      'timestamp_captura': capturadaEn.toUtc().toIso8601String(),
    };
    try {
      await _api.publicar('/ubicaciones-tecnico/', datos);
      return false;
    } on SinConexion {
      await _cola.encolar(ColaOffline.tipoUbicacion, datos);
      return true;
    }
  }
}
