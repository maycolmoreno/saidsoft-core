import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:geolocator/geolocator.dart';

import '../../nucleo/red/api.dart';
import 'repo_gps.dart';

/// Versión del texto que el técnico acepta. Si cambia el texto hay que subirla: el
/// consentimiento se guarda con su versión para poder demostrar QUÉ se aceptó.
const versionTerminos = '1.0';

/// Envío de ubicación durante la jornada.
///
/// Vive a nivel de app y NO dentro de una pantalla: si el temporizador colgara de la
/// pantalla de GPS, el envío se cortaría apenas el técnico cambia de pestaña para
/// trabajar en el mantenimiento — que es exactamente cuando hace falta que reporte.
///
/// El consentimiento es condición previa y no se saltea: el backend rechaza las
/// posiciones sin él, y rastrear a una persona sin su acuerdo no es algo que la app
/// deba hacer "por conveniencia".
class EstadoGps extends ChangeNotifier {
  EstadoGps(this._repo);

  final RepoGps _repo;
  static const _intervalo = Duration(seconds: 30);

  Timer? _temporizador;
  bool _consentimiento = false;
  bool _consultado = false;
  bool _enviando = false;
  String? _error;
  String _ultimo = '';
  int _enviadas = 0;

  bool get consentimiento => _consentimiento;
  bool get consultado => _consultado;
  bool get enviando => _enviando;
  String? get error => _error;
  String get ultimo => _ultimo;
  int get enviadas => _enviadas;

  @override
  void dispose() {
    _temporizador?.cancel();
    super.dispose();
  }

  Future<void> cargarConsentimiento() async {
    _error = null;
    try {
      _consentimiento = await _repo.consentimientoRegistrado();
    } on ErrorApi catch (e) {
      _error = e.mensaje;
    } finally {
      _consultado = true;
      notifyListeners();
    }
  }

  Future<bool> aceptarConsentimiento() async {
    _error = null;
    try {
      await _repo.registrarConsentimiento(versionTerminos: versionTerminos);
      _consentimiento = true;
      notifyListeners();
      return true;
    } on ErrorApi catch (e) {
      _error = e.mensaje;
      notifyListeners();
      return false;
    }
  }

  /// Arranca el envío. Devuelve false si no se pudo (sin consentimiento, sin permiso
  /// o con la ubicación del teléfono apagada) y deja el motivo en [error].
  Future<bool> comenzar() async {
    if (_enviando) return true;
    if (!_consentimiento) {
      _error = 'Falta aceptar el consentimiento de monitoreo.';
      notifyListeners();
      return false;
    }
    if (!await _permisoConcedido()) return false;

    _enviando = true;
    _error = null;
    notifyListeners();
    await _enviarUna();
    _temporizador = Timer.periodic(_intervalo, (_) => _enviarUna());
    return true;
  }

  void detener() {
    _temporizador?.cancel();
    _temporizador = null;
    _enviando = false;
    notifyListeners();
  }

  /// Comprueba permiso y que el GPS del teléfono esté encendido. Sin esto el envío
  /// falla en silencio y el técnico cree que está reportando cuando no.
  Future<bool> _permisoConcedido() async {
    if (!await Geolocator.isLocationServiceEnabled()) {
      _error = 'Activa la ubicacion del telefono.';
      notifyListeners();
      return false;
    }
    var permiso = await Geolocator.checkPermission();
    if (permiso == LocationPermission.denied) {
      permiso = await Geolocator.requestPermission();
    }
    if (permiso == LocationPermission.denied ||
        permiso == LocationPermission.deniedForever) {
      _error = 'Sin permiso de ubicacion no se puede enviar.';
      notifyListeners();
      return false;
    }
    return true;
  }

  Future<void> _enviarUna() async {
    try {
      final posicion = await Geolocator.getCurrentPosition(
        locationSettings: const LocationSettings(
          accuracy: LocationAccuracy.high,
          timeLimit: Duration(seconds: 15),
        ),
      );
      final pendiente = await _repo.enviarUbicacion(
        latitud: posicion.latitude,
        longitud: posicion.longitude,
        precisionMetros: posicion.accuracy,
        capturadaEn: DateTime.now(),
      );
      _enviadas++;
      _ultimo = pendiente ? 'Guardada sin conexion' : 'Enviada';
      _error = null;
    } on ErrorApi catch (e) {
      _error = e.mensaje;
    } catch (_) {
      _error = 'No se pudo obtener la ubicacion.';
    }
    notifyListeners();
  }
}
