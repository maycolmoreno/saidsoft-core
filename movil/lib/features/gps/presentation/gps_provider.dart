import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:geolocator/geolocator.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../data/gps_models.dart';
import '../data/gps_repository.dart';

class GpsProvider extends ChangeNotifier {
  GpsProvider({required GpsRepository repository}) : _repository = repository;

  final GpsRepository _repository;

  // — Consentimiento —
  bool _consentimientoRegistrado = false;
  bool get consentimientoRegistrado => _consentimientoRegistrado;

  bool _loadingConsentimiento = false;
  bool get loadingConsentimiento => _loadingConsentimiento;

  String? _errorConsentimiento;
  String? get errorConsentimiento => _errorConsentimiento;

  Future<void> cargarConsentimientoRegistrado(int tecnicoId) async {
    final prefs = await SharedPreferences.getInstance();
    _consentimientoRegistrado =
        prefs.getBool(_consentimientoKey(tecnicoId)) ?? false;
    notifyListeners();
  }

  Future<bool> registrarConsentimiento(int tecnicoId) async {
    _loadingConsentimiento = true;
    _errorConsentimiento = null;
    notifyListeners();

    try {
      await _repository.registrarConsentimiento(
        ConsentimientoRequest(tecnicoId: tecnicoId),
      );
      _consentimientoRegistrado = true;
      await _guardarConsentimientoLocal(tecnicoId);
      _loadingConsentimiento = false;
      notifyListeners();
      return true;
    } catch (e) {
      final message = e.toString().replaceAll('Exception: ', '');
      if (message.contains('ya tiene un consentimiento de monitoreo activo')) {
        _consentimientoRegistrado = true;
        await _guardarConsentimientoLocal(tecnicoId);
        _loadingConsentimiento = false;
        _errorConsentimiento = null;
        notifyListeners();
        return true;
      }
      _errorConsentimiento = message;
      _loadingConsentimiento = false;
      notifyListeners();
      return false;
    }
  }

  Future<void> _guardarConsentimientoLocal(int tecnicoId) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setBool(_consentimientoKey(tecnicoId), true);
  }

  String _consentimientoKey(int tecnicoId) =>
      'gps_consentimiento_registrado_$tecnicoId';

  // — Envío de ubicación (TECNICO) —
  Timer? _sendTimer;
  bool _enviandoUbicacion = false;
  bool get enviandoUbicacion => _enviandoUbicacion;
  bool _trackingActivo = false;
  bool get trackingActivo => _trackingActivo;
  String? _errorUbicacion;
  String? get errorUbicacion => _errorUbicacion;

  Future<bool> _checkPermissions() async {
    bool serviceEnabled = await Geolocator.isLocationServiceEnabled();
    if (!serviceEnabled) {
      _errorUbicacion = 'Los servicios de ubicación están desactivados.';
      notifyListeners();
      return false;
    }

    LocationPermission permission = await Geolocator.checkPermission();
    if (permission == LocationPermission.denied) {
      permission = await Geolocator.requestPermission();
      if (permission == LocationPermission.denied) {
        _errorUbicacion = 'Permiso de ubicación denegado.';
        notifyListeners();
        return false;
      }
    }
    if (permission == LocationPermission.deniedForever) {
      _errorUbicacion =
          'Permiso de ubicación denegado permanentemente. Habilítelo en ajustes.';
      notifyListeners();
      return false;
    }
    return true;
  }

  Future<void> iniciarTracking(int tecnicoId) async {
    if (_trackingActivo) return;

    final ok = await _checkPermissions();
    if (!ok) return;

    _trackingActivo = true;
    _errorUbicacion = null;
    notifyListeners();

    // Enviar inmediatamente
    await _enviarPosicion(tecnicoId);

    // Repetir cada 30 segundos
    _sendTimer = Timer.periodic(
      const Duration(seconds: 30),
      (_) => _enviarPosicion(tecnicoId),
    );
  }

  Future<void> _enviarPosicion(int tecnicoId) async {
    if (_enviandoUbicacion) return;
    _enviandoUbicacion = true;
    notifyListeners();

    try {
      final position = await Geolocator.getCurrentPosition(
        locationSettings: const LocationSettings(
          accuracy: LocationAccuracy.high,
          timeLimit: Duration(seconds: 10),
        ),
      );
      await _repository.enviarUbicacion(
        UbicacionTecnicoRequest(
          tecnicoId: tecnicoId,
          latitud: position.latitude,
          longitud: position.longitude,
          precisionMetros: position.accuracy,
          timestampCaptura: DateTime.now().toUtc().toIso8601String(),
        ),
      );
      _errorUbicacion = null;
    } catch (e) {
      _errorUbicacion = e.toString().replaceAll('Exception: ', '');
    }

    _enviandoUbicacion = false;
    notifyListeners();
  }

  void detenerTracking() {
    _sendTimer?.cancel();
    _sendTimer = null;
    _trackingActivo = false;
    notifyListeners();
  }

  /// Envía la ubicación una sola vez al ingresar a la app (sin iniciar tracking).
  Future<void> enviarUbicacionAlIngreso(int tecnicoId) async {
    final ok = await _checkPermissions();
    if (!ok) return;
    await _enviarPosicion(tecnicoId);
  }

  // — Consulta tiempo real (ADMIN) —
  List<UbicacionActivaResponse> _ubicacionesActivas = [];
  List<UbicacionActivaResponse> get ubicacionesActivas => _ubicacionesActivas;

  bool _loadingUbicaciones = false;
  bool get loadingUbicaciones => _loadingUbicaciones;

  String? _errorUbicaciones;
  String? get errorUbicaciones => _errorUbicaciones;

  Timer? _refreshTimer;

  Future<void> cargarUbicacionesTiempoReal() async {
    _loadingUbicaciones = true;
    _errorUbicaciones = null;
    notifyListeners();

    try {
      _ubicacionesActivas = await _repository.obtenerUbicacionesTiempoReal();
    } catch (e) {
      _errorUbicaciones = e.toString().replaceAll('Exception: ', '');
    }

    _loadingUbicaciones = false;
    notifyListeners();
  }

  void iniciarRefrescoAutomatico() {
    cargarUbicacionesTiempoReal();
    _refreshTimer = Timer.periodic(
      const Duration(seconds: 30),
      (_) => cargarUbicacionesTiempoReal(),
    );
  }

  void detenerRefrescoAutomatico() {
    _refreshTimer?.cancel();
    _refreshTimer = null;
  }

  @override
  void dispose() {
    detenerTracking();
    detenerRefrescoAutomatico();
    super.dispose();
  }
}
