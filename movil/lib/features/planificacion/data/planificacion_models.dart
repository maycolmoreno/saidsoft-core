class ActividadPlanificada {
  final int? idActividadPlanificada;
  final int tecnicoId;
  final String? tecnicoNombre;
  final int creadoPorId;
  final String? creadoPorNombre;
  final String titulo;
  final String? descripcion;
  final String tipoActividad;
  final String prioridad;
  final String estado;
  final String fechaInicio;
  final String fechaFin;
  final String? fechaCompletada;
  final int? tiempoEstimadoMinutos;
  final int? tiempoRealMinutos;
  final int? referenciaMantenimientoId;
  final String? observaciones;
  final String? creadoEn;

  const ActividadPlanificada({
    this.idActividadPlanificada,
    required this.tecnicoId,
    this.tecnicoNombre,
    required this.creadoPorId,
    this.creadoPorNombre,
    required this.titulo,
    this.descripcion,
    required this.tipoActividad,
    this.prioridad = 'MEDIA',
    this.estado = 'PENDIENTE',
    required this.fechaInicio,
    required this.fechaFin,
    this.fechaCompletada,
    this.tiempoEstimadoMinutos,
    this.tiempoRealMinutos,
    this.referenciaMantenimientoId,
    this.observaciones,
    this.creadoEn,
  });

  factory ActividadPlanificada.fromJson(Map<String, dynamic> json) {
    return ActividadPlanificada(
      idActividadPlanificada: json['idActividadPlanificada'] as int?,
      tecnicoId: json['tecnicoId'] as int? ?? 0,
      tecnicoNombre: json['tecnicoNombre']?.toString(),
      creadoPorId: json['creadoPorId'] as int? ?? 0,
      creadoPorNombre: json['creadoPorNombre']?.toString(),
      titulo: json['titulo']?.toString() ?? '',
      descripcion: json['descripcion']?.toString(),
      tipoActividad: json['tipoActividad']?.toString() ?? '',
      prioridad: json['prioridad']?.toString() ?? 'MEDIA',
      estado: json['estado']?.toString() ?? 'PENDIENTE',
      fechaInicio: json['fechaInicio']?.toString() ?? '',
      fechaFin: json['fechaFin']?.toString() ?? '',
      fechaCompletada: json['fechaCompletada']?.toString(),
      tiempoEstimadoMinutos: json['tiempoEstimadoMinutos'] as int?,
      tiempoRealMinutos: json['tiempoRealMinutos'] as int?,
      referenciaMantenimientoId: json['referenciaMantenimientoId'] as int?,
      observaciones: json['observaciones']?.toString(),
      creadoEn: json['creadoEn']?.toString(),
    );
  }

  Map<String, dynamic> toJson() => {
        'tecnicoId': tecnicoId,
        'creadoPorId': creadoPorId,
        'titulo': titulo,
        'descripcion': descripcion,
        'tipoActividad': tipoActividad,
        'prioridad': prioridad,
        'fechaInicio': fechaInicio,
        'fechaFin': fechaFin,
        'tiempoEstimadoMinutos': tiempoEstimadoMinutos,
        'referenciaMantenimientoId': referenciaMantenimientoId,
        'observaciones': observaciones,
      };

  bool get isVencida => estado == 'VENCIDA';
  bool get isCompletada => estado == 'COMPLETADA';
  bool get isPendiente => estado == 'PENDIENTE';
  bool get isEnProgreso => estado == 'EN_PROGRESO';

  String get tipoLabel => tipoActividad.replaceAll('_', ' ');
  String get estadoLabel => estado.replaceAll('_', ' ');
}

class MetricasCumplimiento {
  final int tecnicoId;
  final String tecnicoNombre;
  final String periodo;
  final int totalActividades;
  final int completadas;
  final int pendientes;
  final int enProgreso;
  final int vencidas;
  final int canceladas;
  final double porcentajeCompletadas;
  final double porcentajeCumplimientoATiempo;
  final int completadasATiempo;
  final int completadasTarde;
  final double tiempoPromedioMinutos;

  const MetricasCumplimiento({
    required this.tecnicoId,
    required this.tecnicoNombre,
    required this.periodo,
    required this.totalActividades,
    required this.completadas,
    required this.pendientes,
    required this.enProgreso,
    required this.vencidas,
    required this.canceladas,
    required this.porcentajeCompletadas,
    required this.porcentajeCumplimientoATiempo,
    required this.completadasATiempo,
    required this.completadasTarde,
    required this.tiempoPromedioMinutos,
  });

  factory MetricasCumplimiento.fromJson(Map<String, dynamic> json) {
    return MetricasCumplimiento(
      tecnicoId: json['tecnicoId'] as int? ?? 0,
      tecnicoNombre: json['tecnicoNombre']?.toString() ?? '',
      periodo: json['periodo']?.toString() ?? 'MENSUAL',
      totalActividades: json['totalActividades'] as int? ?? 0,
      completadas: json['completadas'] as int? ?? 0,
      pendientes: json['pendientes'] as int? ?? 0,
      enProgreso: json['enProgreso'] as int? ?? 0,
      vencidas: json['vencidas'] as int? ?? 0,
      canceladas: json['canceladas'] as int? ?? 0,
      porcentajeCompletadas:
          (json['porcentajeCompletadas'] as num?)?.toDouble() ?? 0,
      porcentajeCumplimientoATiempo:
          (json['porcentajeCumplimientoATiempo'] as num?)?.toDouble() ?? 0,
      completadasATiempo: json['completadasATiempo'] as int? ?? 0,
      completadasTarde: json['completadasTarde'] as int? ?? 0,
      tiempoPromedioMinutos:
          (json['tiempoPromedioMinutos'] as num?)?.toDouble() ?? 0,
    );
  }
}
