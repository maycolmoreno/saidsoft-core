class LoginRequest {
  final String username;
  final String password;

  const LoginRequest({
    required this.username,
    required this.password,
  });

  Map<String, dynamic> toJson() => {
        'username': username,
        'password': password,
      };
}

enum UserRole {
  admin,
  tecnico,
  consulta,
  unknown,
}

enum UserCapability {
  viewMantenimientos,
  createMantenimiento,
  closeMantenimiento,
  viewPlanificacion,
  viewVisitas,
  viewEquipos,
  viewNotificaciones,
  manageUbicaciones,
  manageServerConfig,
  sendGpsLocation,
  viewGpsRealtime,
}

class RoleCapabilities {
  const RoleCapabilities(this.values);

  final Set<UserCapability> values;

  bool has(UserCapability capability) => values.contains(capability);
}

class AuthSession {
  final String token;
  final String username;
  final String displayName;
  final String role;
  final int? userId;
  final List<String> modules;
  final bool modulesLoaded;

  const AuthSession({
    required this.token,
    required this.username,
    required this.displayName,
    required this.role,
    this.userId,
    this.modules = const [],
    this.modulesLoaded = false,
  });

  factory AuthSession.fromJson(Map<String, dynamic> json) {
    return AuthSession(
      token: json['token']?.toString() ?? json['jwt']?.toString() ?? '',
      username:
          json['username']?.toString() ?? json['usuario']?.toString() ?? '',
      displayName: json['nombre']?.toString() ??
          json['displayName']?.toString() ??
          json['username']?.toString() ??
          '',
      role: json['role']?.toString() ?? json['rol']?.toString() ?? '',
      modules: (json['modules'] as List? ?? json['modulos'] as List? ?? const [])
          .map((item) => item?.toString() ?? '')
          .where((item) => item.trim().isNotEmpty)
          .toList(),
      modulesLoaded: json.containsKey('modules') || json.containsKey('modulos'),
    );
  }

  String get normalizedRole => role.trim().toUpperCase();
  Set<String> get normalizedModules => modules
      .map((item) => item.trim().toUpperCase())
      .where((item) => item.isNotEmpty)
      .toSet();

  UserRole get userRole {
    final value = normalizedRole;
    if (value.contains('ADMIN')) {
      return UserRole.admin;
    }
    if (value.contains('TECNICO') || value.contains('SOPORTE')) {
      return UserRole.tecnico;
    }
    if (value.contains('CONSULTA') ||
        value.contains('AUDITOR') ||
        value.contains('INVITADO') ||
        value.contains('CUSTODIO')) {
      return UserRole.consulta;
    }
    return UserRole.unknown;
  }

  String get roleLabel {
    switch (userRole) {
      case UserRole.admin:
        return 'Administrador';
      case UserRole.tecnico:
        return 'Tecnico';
      case UserRole.consulta:
        return 'Consulta';
      case UserRole.unknown:
        return normalizedRole.isEmpty ? 'Sin rol' : normalizedRole;
    }
  }

  bool get isSupported => userRole != UserRole.unknown;

  RoleCapabilities get capabilities {
    final moduleCapabilities = _moduleCapabilities();
    if (modulesLoaded) {
      moduleCapabilities.addAll(_localRoleCapabilities());
      return RoleCapabilities(moduleCapabilities);
    }

    switch (userRole) {
      case UserRole.admin:
        return const RoleCapabilities({
          UserCapability.viewMantenimientos,
          UserCapability.createMantenimiento,
          UserCapability.closeMantenimiento,
          UserCapability.viewPlanificacion,
          UserCapability.viewVisitas,
          UserCapability.viewEquipos,
          UserCapability.viewNotificaciones,
          UserCapability.manageUbicaciones,
          UserCapability.manageServerConfig,
          UserCapability.viewGpsRealtime,
        });
      case UserRole.tecnico:
        return const RoleCapabilities({
          UserCapability.viewMantenimientos,
          UserCapability.createMantenimiento,
          UserCapability.closeMantenimiento,
          UserCapability.viewPlanificacion,
          UserCapability.viewVisitas,
          UserCapability.viewEquipos,
          UserCapability.viewNotificaciones,
          UserCapability.sendGpsLocation,
        });
      case UserRole.consulta:
        return const RoleCapabilities({
          UserCapability.viewMantenimientos,
          UserCapability.viewEquipos,
          UserCapability.viewNotificaciones,
        });
      case UserRole.unknown:
        return const RoleCapabilities({});
    }
  }

  Set<UserCapability> _moduleCapabilities() {
    final values = <UserCapability>{};
    for (final module in normalizedModules) {
      switch (module) {
        case 'MANTENIMIENTO':
          values.addAll(const {
            UserCapability.viewMantenimientos,
            UserCapability.createMantenimiento,
            UserCapability.closeMantenimiento,
          });
          break;
        case 'PLANIFICACION':
          values.add(UserCapability.viewPlanificacion);
          break;
        case 'VISITA_TECNICA':
          values.add(UserCapability.viewVisitas);
          break;
        case 'EQUIPOS':
          values.add(UserCapability.viewEquipos);
          break;
        case 'NOTIFICACIONES':
          values.add(UserCapability.viewNotificaciones);
          break;
        case 'UBICACIONES':
          values.add(UserCapability.manageUbicaciones);
          break;
        case 'MONITOREO_GPS':
          values.add(UserCapability.sendGpsLocation);
          break;
        case 'GPS_TIEMPO_REAL':
          values.add(UserCapability.viewGpsRealtime);
          break;
        default:
          break;
      }
    }
    return values;
  }

  Set<UserCapability> _localRoleCapabilities() {
    switch (userRole) {
      case UserRole.admin:
        return const {UserCapability.manageServerConfig};
      case UserRole.tecnico:
      case UserRole.consulta:
      case UserRole.unknown:
        return const {};
    }
  }
}
