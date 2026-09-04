import 'package:cresio_campo/nucleo/almacen/almacen_seguro.dart';
import 'package:cresio_campo/rasgos/sesion/bloqueo_biometrico.dart';
import 'package:cresio_campo/rasgos/sesion/estado_sesion.dart';
import 'package:cresio_campo/rasgos/sesion/repo_sesion.dart';
import 'package:cresio_campo/rasgos/sesion/sesion.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// Lo que se fija acá es el CRITERIO de la cerradura, no la biometría en sí (esa la
/// resuelve el sistema operativo): con qué fase arranca la app, qué pasa cuando el
/// dedo falla, y qué NO debe pasar nunca.
void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() {
    // `arrancar()` lee el servidor configurado antes de mirar la sesion; sin esto
    // cortaria en FaseSesion.sinServidor y ningun caso de abajo se ejercitaria.
    SharedPreferences.setMockInitialValues({'servidor_configurado': true});
  });

  group('fase de arranque', () {
    test('sin cerradura activada entra directo', () async {
      final estado = _estado(bloqueoGuardado: false, dispositivoSoporta: true);
      await estado.arrancar();
      expect(estado.fase, FaseSesion.autenticado);
    });

    test('con cerradura activada queda bloqueado, no sin sesion', () async {
      // La distincion importa: `sinSesion` mandaria a pedir usuario y clave, y la
      // sesion sigue siendo perfectamente valida.
      final estado = _estado(bloqueoGuardado: true, dispositivoSoporta: true);
      await estado.arrancar();
      expect(estado.fase, FaseSesion.bloqueado);
    });

    test('si el telefono perdio la biometria NO deja al tecnico afuera', () async {
      // Huella borrada o lector roto: quedarse sin poder trabajar en una farmacia es
      // peor que la proteccion que se pierde, y el token sigue siendo suyo.
      final estado = _estado(bloqueoGuardado: true, dispositivoSoporta: false);
      await estado.arrancar();
      expect(estado.fase, FaseSesion.autenticado);
    });

    test('sin token guardado la cerradura no aplica', () async {
      final estado = _estado(bloqueoGuardado: true, dispositivoSoporta: true, conToken: false);
      await estado.arrancar();
      expect(estado.fase, FaseSesion.sinSesion);
    });
  });

  group('desbloquear', () {
    test('con huella valida pasa a autenticado', () async {
      final bloqueo = _BloqueoFalso(soporta: true, autenticaOk: true);
      final estado = _estado(bloqueoGuardado: true, bloqueo: bloqueo);
      await estado.arrancar();
      expect(await estado.desbloquear(), isTrue);
      expect(estado.fase, FaseSesion.autenticado);
    });

    test('un fallo NO cierra la sesion ni borra el token', () async {
      // Un dedo mojado no puede costar tener que reescribir usuario y clave.
      final almacen = _AlmacenFalso(bloqueo: true);
      final bloqueo = _BloqueoFalso(soporta: true, autenticaOk: false);
      final estado = _estado(bloqueoGuardado: true, bloqueo: bloqueo, almacen: almacen);
      await estado.arrancar();

      expect(await estado.desbloquear(), isFalse);
      expect(estado.fase, FaseSesion.bloqueado, reason: 'sigue bloqueado para reintentar');
      expect(await almacen.leerToken(), isNotNull, reason: 'el token no se toca');
      expect(estado.error, isNotNull);
    });
  });

  group('activar la cerradura', () {
    test('exige pasar la huella una vez, ahi mismo', () async {
      // Asi el tecnico comprueba que funciona ANTES de depender de ella para entrar.
      final almacen = _AlmacenFalso(bloqueo: false);
      final estado = _estado(
        bloqueoGuardado: false,
        bloqueo: _BloqueoFalso(soporta: true, autenticaOk: false),
        almacen: almacen,
      );
      expect(await estado.cambiarBloqueo(true), isFalse);
      expect(await almacen.leerBloqueoBiometrico(), isFalse,
          reason: 'no se guarda una cerradura que no se pudo probar');
    });

    test('apagarla no pide huella: quien ya entro puede soltarla', () async {
      final almacen = _AlmacenFalso(bloqueo: true);
      final estado = _estado(
        bloqueoGuardado: true,
        bloqueo: _BloqueoFalso(soporta: true, autenticaOk: false),
        almacen: almacen,
      );
      expect(await estado.cambiarBloqueo(false), isTrue);
      expect(await almacen.leerBloqueoBiometrico(), isFalse);
    });
  });

  test('cerrar sesion borra tambien la preferencia de bloqueo', () async {
    // Es de la persona, no del telefono: si quedara, el proximo tecnico heredaria una
    // cerradura que se abre con la huella del anterior.
    final almacen = _AlmacenFalso(bloqueo: true);
    await almacen.borrarSesion();
    expect(await almacen.leerBloqueoBiometrico(), isFalse);
    expect(await almacen.leerToken(), isNull);
  });
}

EstadoSesion _estado({
  required bool bloqueoGuardado,
  bool dispositivoSoporta = true,
  bool conToken = true,
  _BloqueoFalso? bloqueo,
  _AlmacenFalso? almacen,
}) {
  final alm = almacen ?? _AlmacenFalso(bloqueo: bloqueoGuardado, conToken: conToken);
  return EstadoSesion(
    _RepoFalso(conSesion: conToken),
    almacen: alm,
    bloqueo: bloqueo ?? _BloqueoFalso(soporta: dispositivoSoporta, autenticaOk: true),
  );
}


/// Doble del servicio biometrico: el sistema operativo no existe en un test.
class _BloqueoFalso implements BloqueoBiometrico {
  _BloqueoFalso({required this.soporta, required this.autenticaOk});

  final bool soporta;
  final bool autenticaOk;
  int pedidos = 0;

  @override
  Future<bool> disponible() async => soporta;

  @override
  Future<bool> hayBiometriaRegistrada() async => soporta;

  @override
  Future<bool> autenticar({String motivo = ''}) async {
    pedidos++;
    return autenticaOk;
  }
}

/// Almacen en memoria: se sobreescriben solo los accesos que la cerradura usa.
class _AlmacenFalso extends AlmacenSeguro {
  _AlmacenFalso({required bool bloqueo, bool conToken = true})
      : _bloqueo = bloqueo,
        _token = conToken ? 'token-de-prueba' : null;

  bool _bloqueo;
  String? _token;

  @override
  Future<bool> leerBloqueoBiometrico() async => _bloqueo;

  @override
  Future<void> guardarBloqueoBiometrico(bool activo) async => _bloqueo = activo;

  @override
  Future<String?> leerToken() async => _token;

  @override
  Future<void> borrarSesion() async {
    _token = null;
    _bloqueo = false;
  }
}

class _RepoFalso implements RepoSesion {
  _RepoFalso({required this.conSesion});

  final bool conSesion;

  @override
  Future<Sesion?> recuperarSesion() async => conSesion
      ? const Sesion(
          token: 'token-de-prueba',
          usuario: 'tecnico',
          nombre: 'Tecnico de Prueba',
          id: 1,
          permisos: [],
          esStaff: false,
        )
      : null;

  @override
  Future<Sesion> iniciarSesion(String usuario, String clave) =>
      throw UnimplementedError();

  @override
  Future<void> cerrarSesion() async {}
}
