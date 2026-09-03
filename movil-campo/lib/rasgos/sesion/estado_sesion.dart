import 'package:flutter/foundation.dart';
import 'package:permission_handler/permission_handler.dart';

import '../../nucleo/config.dart';
import '../../nucleo/red/api.dart';
import 'repo_sesion.dart';
import 'sesion.dart';

enum FaseSesion { cargando, sinServidor, sinSesion, autenticado }

class EstadoSesion extends ChangeNotifier {
  EstadoSesion(this._repo);

  final RepoSesion _repo;

  FaseSesion _fase = FaseSesion.cargando;
  Sesion? _sesion;
  String? _error;
  bool _ocupado = false;

  FaseSesion get fase => _fase;
  Sesion? get sesion => _sesion;
  String? get error => _error;
  bool get ocupado => _ocupado;

  bool puede(Permiso permiso) => _sesion?.puede(permiso) ?? false;

  /// Decide la primera pantalla. Sin servidor configurado no tiene sentido intentar
  /// nada más: la app no sabe contra qué hablar.
  Future<void> arrancar() async {
    await Config.cargar();
    if (!Config.configurado) {
      _fase = FaseSesion.sinServidor;
      notifyListeners();
      return;
    }
    _sesion = await _repo.recuperarSesion();
    _fase = _sesion == null ? FaseSesion.sinSesion : FaseSesion.autenticado;
    notifyListeners();
  }

  /// Tras guardar la configuración del servidor.
  void servidorConfigurado() {
    _fase = _sesion == null ? FaseSesion.sinSesion : FaseSesion.autenticado;
    notifyListeners();
  }

  Future<bool> iniciarSesion(String usuario, String clave) async {
    if (usuario.trim().isEmpty || clave.isEmpty) {
      _error = 'Ingresa usuario y clave.';
      notifyListeners();
      return false;
    }
    _ocupado = true;
    _error = null;
    notifyListeners();
    try {
      _sesion = await _repo.iniciarSesion(usuario.trim(), clave);
      _fase = FaseSesion.autenticado;
      await _pedirPermisos();
      return true;
    } on ErrorApi catch (e) {
      _error = e.mensaje;
      return false;
    } finally {
      _ocupado = false;
      notifyListeners();
    }
  }

  /// Pide ubicación y notificaciones al entrar, no cuando ya hacen falta.
  ///
  /// Pedirlos recién al registrar la llegada significaría interrumpir al técnico
  /// justo cuando llegó a la farmacia, y si los rechaza en ese momento la visita
  /// queda sin verificar. Acá tiene tiempo de decidir.
  ///
  /// Nunca lanza ni bloquea: si los rechaza, la app funciona igual -- solo pierde la
  /// verificación de presencia y los avisos, y cada pantalla lo dice cuando toca.
  Future<void> _pedirPermisos() async {
    try {
      await [Permission.location, Permission.notification].request();
    } catch (_) {
      // Sin permisos la app sigue siendo usable; no hay nada que reportar acá.
    }
  }

  Future<void> cerrarSesion() async {
    await _repo.cerrarSesion();
    _sesion = null;
    _fase = FaseSesion.sinSesion;
    notifyListeners();
  }

  /// La llama el cliente de API cuando el servidor devuelve 401: el token dejó de
  /// valer y hay que volver a la pantalla de ingreso sin que el técnico quede
  /// tocando botones que ya no responden.
  void sesionExpirada() {
    _sesion = null;
    _fase = FaseSesion.sinSesion;
    _error = 'Tu sesion expiro. Volve a iniciar sesion.';
    notifyListeners();
  }
}
