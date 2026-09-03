import 'package:flutter_test/flutter_test.dart';

import 'package:cresio_campo/rasgos/sesion/sesion.dart';

/// Los permisos vienen de Django: son los MISMOS codenames que evalúa el panel web,
/// así que la app y la web no pueden habilitar cosas distintas.
void main() {
  Sesion conPermisos(List<String> permisos, {bool staff = false}) => Sesion(
        token: 'tok',
        usuario: 'romo',
        nombre: 'Ronald Moreno',
        id: 1,
        permisos: permisos,
        esStaff: staff,
      );

  group('permisos', () {
    test('habilita solo lo que el usuario tiene', () {
      final s = conPermisos(const [
        'mantenimiento.view_mantenimiento',
        'mantenimiento.change_mantenimiento',
      ]);
      expect(s.puede(Permiso.verMantenimientos), isTrue);
      expect(s.puede(Permiso.cerrarMantenimiento), isTrue);
      expect(s.puede(Permiso.crearMantenimiento), isFalse);
      expect(s.puede(Permiso.verVisitas), isFalse);
    });

    test('sin permisos no puede nada', () {
      final s = conPermisos(const []);
      for (final p in Permiso.values) {
        expect(s.puede(p), isFalse, reason: '$p no deberia estar habilitado');
      }
    });

    test('los permisos ajenos a la app no habilitan nada', () {
      final s = conPermisos(const ['admin.add_logentry', 'sessions.delete_session']);
      for (final p in Permiso.values) {
        expect(s.puede(p), isFalse);
      }
    });
  });

  group('identidad', () {
    test('el rol se deriva de es_staff, no de un texto libre', () {
      // Regresión: mandar un rol como texto ("staff") y compararlo por nombre es
      // frágil; acá el backend dice si es staff y la app solo lo etiqueta.
      expect(conPermisos(const [], staff: true).etiquetaRol, 'Administrador');
      expect(conPermisos(const [], staff: false).etiquetaRol, 'Tecnico');
    });

    test('las iniciales salen del nombre y nunca quedan vacias', () {
      expect(conPermisos(const []).iniciales, 'RM');
      expect(
        const Sesion(
          token: 't', usuario: 'jperez', nombre: '', id: 1,
          permisos: [], esStaff: false,
        ).iniciales,
        'J',
      );
    });

    test('desdeJson tolera un backend que omita campos', () {
      final s = Sesion.desdeJson('tok', const {'username': 'ana'});
      expect(s.usuario, 'ana');
      // Sin "nombre" cae al usuario en vez de quedar en blanco.
      expect(s.nombre, 'ana');
      expect(s.permisos, isEmpty);
      expect(s.esStaff, isFalse);
    });
  });
}
