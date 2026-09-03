class Ubicacion {
  const Ubicacion({
    required this.id,
    required this.nombre,
    required this.agencia,
    required this.ciudad,
    required this.direccion,
    required this.estado,
  });

  final int id;
  final String nombre;
  final String agencia;
  final String ciudad;
  final String direccion;
  final bool estado;

  factory Ubicacion.fromJson(Map<String, dynamic> json) {
    return Ubicacion(
      id: _asInt(json['idUbicacion']) != 0 ? _asInt(json['idUbicacion']) : _asInt(json['id']),
      nombre: _text(json['nombre'], fallback: 'Sin nombre'),
      agencia: _text(json['agencia'], fallback: ''),
      ciudad: _text(json['ciudad'], fallback: ''),
      direccion: _text(json['direccion'], fallback: ''),
      estado: json['estado'] == true,
    );
  }

  Map<String, dynamic> toCreateJson({
    required String nombre,
    required String agencia,
    String? ciudad,
    String? direccion,
  }) {
    return {
      'nombre': nombre,
      'agencia': agencia,
      'ciudad': ciudad,
      'direccion': direccion,
      'estado': true,
    };
  }

  Map<String, dynamic> toUpdateJson({
    required String nombre,
    required String agencia,
    String? ciudad,
    String? direccion,
    required bool estado,
  }) {
    return {
      'idUbicacion': id,
      'nombre': nombre,
      'agencia': agencia,
      'ciudad': ciudad,
      'direccion': direccion,
      'estado': estado,
    };
  }
}

int _asInt(dynamic value) {
  if (value is int) {
    return value;
  }
  if (value is num) {
    return value.toInt();
  }
  return int.tryParse(value?.toString() ?? '') ?? 0;
}

String _text(dynamic value, {String fallback = ''}) {
  final text = value?.toString().trim() ?? '';
  return text.isEmpty ? fallback : text;
}
