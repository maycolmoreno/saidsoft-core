class UbicacionTecnicoRequest {
  final int tecnicoId;
  final double latitud;
  final double longitud;
  final double? precisionMetros;
  final String? timestampCaptura;

  const UbicacionTecnicoRequest({
    required this.tecnicoId,
    required this.latitud,
    required this.longitud,
    this.precisionMetros,
    this.timestampCaptura,
  });

  Map<String, dynamic> toJson() => {
        'tecnicoId': tecnicoId,
        'latitud': latitud,
        'longitud': longitud,
        if (precisionMetros != null) 'precisionMetros': precisionMetros,
        if (timestampCaptura != null) 'timestampCaptura': timestampCaptura,
      };
}

class ConsentimientoRequest {
  final int tecnicoId;
  final String? versionTerminos;

  const ConsentimientoRequest({
    required this.tecnicoId,
    this.versionTerminos,
  });

  Map<String, dynamic> toJson() => {
        'tecnicoId': tecnicoId,
        if (versionTerminos != null) 'versionTerminos': versionTerminos,
      };
}

class UbicacionActivaResponse {
  final int usuarioId;
  final String nombre;
  final String? departamento;
  final double latitud;
  final double longitud;
  final double? precisionMetros;
  final int minutosAtras;

  const UbicacionActivaResponse({
    required this.usuarioId,
    required this.nombre,
    this.departamento,
    required this.latitud,
    required this.longitud,
    this.precisionMetros,
    required this.minutosAtras,
  });

  factory UbicacionActivaResponse.fromJson(Map<String, dynamic> json) {
    return UbicacionActivaResponse(
      usuarioId: json['usuarioId'] as int,
      nombre: json['nombre']?.toString() ?? '',
      departamento: json['departamento']?.toString(),
      latitud: (json['latitud'] as num).toDouble(),
      longitud: (json['longitud'] as num).toDouble(),
      precisionMetros: (json['precisionMetros'] as num?)?.toDouble(),
      minutosAtras: json['minutosAtras'] as int? ?? 0,
    );
  }
}
