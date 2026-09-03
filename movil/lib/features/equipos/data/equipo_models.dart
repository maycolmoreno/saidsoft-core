class EquipoListItem {
  const EquipoListItem({
    required this.id,
    required this.codigoSap,
    required this.modelo,
    required this.serial,
    required this.estadoEquipo,
    required this.procesador,
    required this.mac,
    required this.custodioNombre,
    required this.ubicacionNombre,
    required this.estadoMantenimiento,
    required this.diasSinMantenimiento,
  });

  final int id;
  final String codigoSap;
  final String modelo;
  final String serial;
  final String estadoEquipo;
  final String procesador;
  final String mac;
  final String custodioNombre;
  final String ubicacionNombre;
  final String estadoMantenimiento;
  final int diasSinMantenimiento;

  factory EquipoListItem.fromJson(Map<String, dynamic> json) {
    return EquipoListItem(
      id: _asInt(json['idEquipo']) != 0
          ? _asInt(json['idEquipo'])
          : _asInt(json['id']),
      codigoSap: _text(json['codigoSap'], fallback: 'Sin codigo'),
      modelo: _text(json['modelo'], fallback: '-'),
      serial: _text(json['serial'], fallback: '-'),
      estadoEquipo: _text(json['estadoEquipo'], fallback: '-'),
      procesador: _text(json['procesador'], fallback: ''),
      mac: _text(json['mac'], fallback: ''),
      custodioNombre: _text(json['custodioNombre'], fallback: ''),
      ubicacionNombre: _text(json['ubicacionNombre'], fallback: ''),
      estadoMantenimiento: _text(json['estadoMantenimiento'], fallback: ''),
      diasSinMantenimiento: _asInt(json['diasSinMantenimiento']),
    );
  }
}

class EquipoHistorial {
  const EquipoHistorial({
    required this.equipo,
    required this.estadisticas,
    required this.estadoMantenimiento,
    required this.mantenimientos,
  });

  final EquipoDetalle equipo;
  final EquipoEstadisticas estadisticas;
  final String estadoMantenimiento;
  final List<EquipoMantenimientoResumen> mantenimientos;

  factory EquipoHistorial.fromJson(Map<String, dynamic> json) {
    return EquipoHistorial(
      equipo: EquipoDetalle.fromJson(
        Map<String, dynamic>.from(json['equipo'] as Map? ?? const {}),
      ),
      estadisticas: EquipoEstadisticas.fromJson(
        Map<String, dynamic>.from(json['estadisticas'] as Map? ?? const {}),
      ),
      estadoMantenimiento: _text(json['estadoMantenimiento'], fallback: '-'),
      mantenimientos: (json['mantenimientos'] as List? ?? const [])
          .map((item) => EquipoMantenimientoResumen.fromJson(
              Map<String, dynamic>.from(item as Map)))
          .toList(),
    );
  }
}

class EquipoDetalle {
  const EquipoDetalle({
    required this.codigoSap,
    required this.marca,
    required this.modelo,
    required this.serial,
    required this.estadoEquipo,
    required this.categoriaNombre,
    required this.procesador,
    required this.memoriaRamGb,
    required this.capacidadAlmacenamientoGb,
    required this.mac,
    required this.licenciaWindowsActivada,
    required this.fechaCompra,
    required this.observacionEquipo,
    required this.custodioNombre,
    required this.departamentoNombre,
    required this.ubicacionNombre,
    required this.ubicacionCiudad,
    required this.fechaInicioCustodio,
  });

  final String codigoSap;
  final String marca;
  final String modelo;
  final String serial;
  final String estadoEquipo;
  final String categoriaNombre;
  final String procesador;
  final String memoriaRamGb;
  final String capacidadAlmacenamientoGb;
  final String mac;
  final bool? licenciaWindowsActivada;
  final String fechaCompra;
  final String observacionEquipo;
  final String custodioNombre;
  final String departamentoNombre;
  final String ubicacionNombre;
  final String ubicacionCiudad;
  final String fechaInicioCustodio;

  factory EquipoDetalle.fromJson(Map<String, dynamic> json) {
    return EquipoDetalle(
      codigoSap: _text(json['codigoSap'], fallback: 'Sin codigo'),
      marca: _text(json['marca']),
      modelo: _text(json['modelo']),
      serial: _text(json['serial']),
      estadoEquipo: _text(json['estadoEquipo']),
      categoriaNombre: _text(json['categoriaNombre']),
      procesador: _text(json['procesador']),
      memoriaRamGb: _text(json['memoriaRamGb']),
      capacidadAlmacenamientoGb: _text(json['capacidadAlmacenamientoGb']),
      mac: _text(json['mac']),
      licenciaWindowsActivada: _asBool(json['licenciaWindowsActivada']),
      fechaCompra: _text(json['fechaCompra']),
      observacionEquipo: _text(json['observacionEquipo']),
      custodioNombre: _text(json['custodioNombre']),
      departamentoNombre: _text(json['departamentoNombre']),
      ubicacionNombre: _text(json['ubicacionNombre']),
      ubicacionCiudad: _text(json['ubicacionCiudad']),
      fechaInicioCustodio: _text(json['fechaInicioCustodio']),
    );
  }
}

class EquipoEstadisticas {
  const EquipoEstadisticas({
    required this.totalMantenimientos,
    required this.totalCerrados,
    required this.totalEnProceso,
    required this.diasSinMantenimiento,
    required this.promedioDiasEntreMantenimientos,
  });

  final int totalMantenimientos;
  final int totalCerrados;
  final int totalEnProceso;
  final int diasSinMantenimiento;
  final int promedioDiasEntreMantenimientos;

  factory EquipoEstadisticas.fromJson(Map<String, dynamic> json) {
    return EquipoEstadisticas(
      totalMantenimientos: _asInt(json['totalMantenimientos']),
      totalCerrados: _asInt(json['totalCerrados']),
      totalEnProceso: _asInt(json['totalEnProceso']),
      diasSinMantenimiento: _asInt(json['diasSinMantenimiento']),
      promedioDiasEntreMantenimientos:
          _asInt(json['promedioDiasEntreMantenimientos']),
    );
  }
}

class EquipoMantenimientoResumen {
  const EquipoMantenimientoResumen({
    required this.tipoInferido,
    required this.descripcion,
    required this.tecnicoNombre,
    required this.estadoInterno,
    required this.fechaCierre,
  });

  final String tipoInferido;
  final String descripcion;
  final String tecnicoNombre;
  final String estadoInterno;
  final String fechaCierre;

  factory EquipoMantenimientoResumen.fromJson(Map<String, dynamic> json) {
    return EquipoMantenimientoResumen(
      tipoInferido: _text(json['tipoInferido']),
      descripcion: _text(json['descripcion']),
      tecnicoNombre: _text(json['tecnicoNombre']),
      estadoInterno: _text(json['estadoInterno']),
      fechaCierre: _text(json['fechaCierre'], fallback: '-'),
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

bool? _asBool(dynamic value) {
  if (value is bool) {
    return value;
  }
  if (value == null) {
    return null;
  }
  final text = value.toString().trim().toLowerCase();
  if (text == 'true') {
    return true;
  }
  if (text == 'false') {
    return false;
  }
  return null;
}

String _text(dynamic value, {String fallback = ''}) {
  final text = value?.toString().trim() ?? '';
  return text.isEmpty ? fallback : text;
}
