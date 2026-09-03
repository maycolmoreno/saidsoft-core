import 'package:flutter_test/flutter_test.dart';

import 'package:cresio_mobile/features/auth/data/auth_models.dart';

/// `/auth/yo/` devuelve los codenames de permiso de Django, no los nombres de módulo
/// de InvTICS. Cuando eso no se traducía, la sesión quedaba sin ninguna capacidad y
/// la app se veía "solo lectura" aunque el usuario tuviera todos los permisos.
void main() {
  AuthSession sesion({
    required String role,
    required List<String> permisos,
  }) {
    return AuthSession(
      token: 'tok',
      username: 'romo',
      displayName: 'romo',
      role: role,
      userId: 1,
      modules: permisos,
      modulesLoaded: true,
    );
  }

  test('un staff con todos los permisos obtiene todas las capacidades', () {
    final s = sesion(role: 'admin', permisos: const [
      'mantenimiento.view_mantenimiento',
      'mantenimiento.add_mantenimiento',
      'mantenimiento.change_mantenimiento',
      'mantenimiento.view_actividadplanificada',
      'mantenimiento.view_visitatecnica',
      'mantenimiento.view_notificacion',
      'mantenimiento.add_ubicaciontecnico',
      'mantenimiento.view_ubicaciontecnico',
      'activos.view_activo',
      'activos.change_ubicacion',
    ]);

    for (final capacidad in UserCapability.values) {
      expect(
        s.capabilities.has(capacidad),
        isTrue,
        reason: 'falta $capacidad',
      );
    }
  });

  test('un tecnico con permisos de solo lectura no puede cerrar', () {
    final s = sesion(role: 'tecnico', permisos: const [
      'mantenimiento.view_mantenimiento',
      'activos.view_activo',
    ]);

    expect(s.capabilities.has(UserCapability.viewMantenimientos), isTrue);
    expect(s.capabilities.has(UserCapability.viewEquipos), isTrue);
    expect(s.capabilities.has(UserCapability.closeMantenimiento), isFalse);
    expect(s.capabilities.has(UserCapability.createMantenimiento), isFalse);
  });

  test('los permisos ajenos a la app se ignoran sin romper', () {
    final s = sesion(role: 'tecnico', permisos: const [
      'admin.add_logentry',
      'sessions.delete_session',
      'mantenimiento.view_mantenimiento',
    ]);

    expect(s.capabilities.has(UserCapability.viewMantenimientos), isTrue);
    expect(s.capabilities.has(UserCapability.viewEquipos), isFalse);
  });

  test('el rol "staff" no es reconocido: por eso se envia "admin"', () {
    // Regresión: con role='staff' userRole caía en unknown, el respaldo por rol
    // quedaba vacío y el badge mostraba "STAFF" en vez de "Administrador".
    expect(sesion(role: 'staff', permisos: const []).userRole, UserRole.unknown);
    expect(sesion(role: 'admin', permisos: const []).userRole, UserRole.admin);
    expect(sesion(role: 'admin', permisos: const []).roleLabel, 'Administrador');
  });
}
