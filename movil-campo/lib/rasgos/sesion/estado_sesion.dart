import 'package:flutter/foundation.dart';
import 'package:permission_handler/permission_handler.dart';

import '../../nucleo/almacen/almacen_seguro.dart';
import '../../nucleo/config.dart';
import '../../nucleo/red/api.dart';
import 'bloqueo_biometrico.dart';
import 'repo_sesion.dart';
import 'sesion.dart';

/// `bloqueado` es distinto de `sinSesion`: la sesión ES válida y el token está
/// guardado, solo falta que el dueño del teléfono se identifique para usarla.
enum FaseSesion { cargando, sinServidor, sinSesion, bloqueado, autenticado }

class EstadoSesion extends ChangeNotifier {
  EstadoSesion(this._repo, {required AlmacenSeguro almacen, BloqueoBiometrico? bloqueo})
      : _almacen = almacen,
        _bloqueo = bloqueo ?? BloqueoBiometrico();

  final RepoSesion _repo;
  final AlmacenSeguro _almacen;
  final BloqueoBiometrico _bloqueo;

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
    if (_sesion == null) {
      _fase = FaseSesion.sinSesion;
      notifyListeners();
      return;
    }
    // Con la cerradura activada la sesión queda retenida hasta que el técnico se
    // identifique. Si el teléfono perdió la biometría (huella borrada, lector roto)
    // NO se lo deja afuera: se entra igual, porque quedarse sin poder trabajar en una
    // farmacia es peor que la protección que se pierde, y el token sigue siendo suyo.
    final conCerradura = await _almacen.leerBloqueoBiometrico();
    _fase = conCerradura && await _bloqueo.disponible()
        ? FaseSesion.bloqueado
        : FaseSesion.autenticado;
    notifyListeners();
  }

  /// Pide la huella para liberar la sesión ya guardada.
  ///
  /// Un fallo NO cierra la sesión ni borra el token: deja la pantalla de bloqueo para
  /// reintentar. Cerrar sesión ante un dedo mojado obligaría a reescribir usuario y
  /// clave en el peor momento.
  Future<bool> desbloquear() async {
    _ocupado = true;
    _error = null;
    notifyListeners();
    try {
      if (await _bloqueo.autenticar()) {
        _fase = FaseSesion.autenticado;
        return true;
      }
      _error = 'No se pudo verificar tu identidad.';
      return false;
    } finally {
      _ocupado = false;
      notifyListeners();
    }
  }

  /// Salida de emergencia desde la pantalla de bloqueo: si la biometría dejó de
  /// funcionar del todo, el técnico vuelve a usuario y clave en vez de quedar preso.
  Future<void> olvidarSesionBloqueada() => cerrarSesion();

  bool get bloqueoActivo => _bloqueoActivo;
  bool _bloqueoActivo = false;

  /// Lee la preferencia y si el teléfono puede cumplirla. Se llama al abrir el menú.
  Future<void> refrescarBloqueo() async {
    _bloqueoActivo = await _almacen.leerBloqueoBiometrico();
    notifyListeners();
  }

  /// True si el teléfono tiene huella/rostro YA registrado: sin eso, ofrecer la
  /// opción sería ofrecer un interruptor que no hace nada.
  Future<bool> biometriaUsable() async =>
      await _bloqueo.disponible() && await _bloqueo.hayBiometriaRegistrada();

  /// Activar exige pasar la huella una vez, ahí mismo: así el técnico comprueba que
  /// funciona ANTES de depender de ella para volver a entrar.
  Future<bool> cambiarBloqueo(bool activar) async {
    if (activar && !await _bloqueo.autenticar(motivo: 'Confirmá tu huella para activar el bloqueo')) {
      return false;
    }
    await _almacen.guardarBloqueoBiometrico(activar);
    _bloqueoActivo = activar;
    notifyListeners();
    return true;
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
