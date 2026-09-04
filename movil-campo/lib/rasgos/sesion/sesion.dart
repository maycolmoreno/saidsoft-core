/// Lo que el técnico puede hacer, derivado de los permisos reales de Django.
///
/// No hay una tabla de roles propia de la app: `/auth/yo/` devuelve los mismos
/// codenames que evalúa el panel web, así que ambos habilitan exactamente lo mismo
/// y no pueden desincronizarse.
enum Permiso {
  verMantenimientos('mantenimiento.view_mantenimiento'),
  crearMantenimiento('mantenimiento.add_mantenimiento'),
  cerrarMantenimiento('mantenimiento.change_mantenimiento'),
  verVisitas('mantenimiento.view_visitatecnica'),
  gestionarVisitas('mantenimiento.change_visitatecnica'),
  enviarUbicacion('mantenimiento.add_ubicaciontecnico'),
  registrarEquipo('activos.add_activo');

  const Permiso(this.codename);

  /// Codename de Django ("app.accion_modelo").
  final String codename;
}

class Sesion {
  const Sesion({
    required this.token,
    required this.usuario,
    required this.nombre,
    required this.id,
    required this.permisos,
    required this.esStaff,
  });

  final String token;
  final String usuario;
  final String nombre;
  final int? id;
  final List<String> permisos;
  final bool esStaff;

  /// Los superusuarios de Django no necesitan permisos explícitos, pero el backend
  /// igual los devuelve todos en `permisos`, así que basta con la lista. `esStaff`
  /// solo se usa para la etiqueta que se muestra.
  bool puede(Permiso permiso) => permisos.contains(permiso.codename);

  String get etiquetaRol => esStaff ? 'Administrador' : 'Tecnico';

  /// Iniciales para el avatar. Nunca vacío: si no hay nombre usa el usuario.
  String get iniciales {
    final base = nombre.trim().isNotEmpty ? nombre.trim() : usuario.trim();
    if (base.isEmpty) return '?';
    final partes = base.split(RegExp(r'\s+')).where((p) => p.isNotEmpty).toList();
    if (partes.length == 1) return partes.first[0].toUpperCase();
    return (partes.first[0] + partes[1][0]).toUpperCase();
  }

  factory Sesion.desdeJson(String token, Map<String, dynamic> json) {
    return Sesion(
      token: token,
      usuario: json['username']?.toString() ?? '',
      nombre: json['nombre']?.toString() ?? json['username']?.toString() ?? '',
      id: json['id'] is int ? json['id'] as int : int.tryParse('${json['id']}'),
      permisos: (json['permisos'] as List? ?? const [])
          .map((p) => p.toString())
          .toList(),
      esStaff: json['es_staff'] == true,
    );
  }
}
