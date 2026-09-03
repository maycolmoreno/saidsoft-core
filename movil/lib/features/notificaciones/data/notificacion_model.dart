class Notificacion {
  const Notificacion({
    required this.id,
    required this.mensaje,
    required this.leida,
    required this.referenciaMantenimientoId,
    required this.creadoEn,
  });

  final int id;
  final String mensaje;
  final bool leida;
  final String referenciaMantenimientoId;
  final String creadoEn;

  factory Notificacion.fromJson(Map<String, dynamic> json) {
    return Notificacion(
      id: _asInt(json['id']),
      mensaje: _text(json['mensaje'], fallback: 'Sin mensaje'),
      leida: json['leida'] == true,
      referenciaMantenimientoId: _text(
        json['referenciaMantenimientoId'],
        fallback: '-',
      ),
      creadoEn: _text(json['creadoEn'], fallback: ''),
    );
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
