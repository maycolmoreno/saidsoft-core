import 'package:intl/intl.dart';

import '../mantenimientos/mantenimiento.dart' show Farmacia;

/// Resultado de cruzar el GPS del técnico contra la farmacia.
///
/// `sinDatos` NO significa que el técnico no haya ido: puede no usar la app, la
/// farmacia puede no tener coordenadas, o puede no haber señal bajo techo. La
/// interfaz tiene que decirlo así.
enum Presencia { verificada, fueraDeRango, sinDatos }

Presencia _presenciaDesde(String? valor) => switch (valor) {
      'verificada' => Presencia.verificada,
      'fuera_de_rango' => Presencia.fueraDeRango,
      _ => Presencia.sinDatos,
    };

class Visita {
  const Visita({
    required this.id,
    required this.estado,
    required this.fechaPlanificada,
    required this.motivo,
    required this.observaciones,
    required this.farmacia,
    required this.presencia,
    required this.distanciaMetros,
    required this.atrasada,
  });

  final int id;
  final String estado;
  final DateTime? fechaPlanificada;
  final String motivo;
  final String observaciones;
  final Farmacia? farmacia;
  final Presencia presencia;
  final double? distanciaMetros;
  final bool atrasada;

  bool get planificada => estado == 'planificada';
  bool get enCurso => estado == 'en_curso';
  bool get realizada => estado == 'realizada';

  String get etiquetaEstado => switch (estado) {
        'planificada' => 'Planificada',
        'en_curso' => 'En curso',
        'realizada' => 'Realizada',
        'cancelada' => 'Cancelada',
        _ => estado,
      };

  String get fechaLegible {
    final f = fechaPlanificada;
    return f == null ? '' : DateFormat('dd/MM/yyyy').format(f);
  }

  factory Visita.desdeJson(Map<String, dynamic> json) => Visita(
        id: json['id'] as int? ?? 0,
        estado: json['estado']?.toString() ?? '',
        fechaPlanificada:
            DateTime.tryParse(json['fecha_planificada']?.toString() ?? ''),
        motivo: json['motivo']?.toString() ?? '',
        observaciones: json['observaciones']?.toString() ?? '',
        farmacia: Farmacia.desdeJson(json['farmacia']),
        presencia: _presenciaDesde(json['presencia_en_sitio']?.toString()),
        distanciaMetros:
            (json['distancia_verificacion_metros'] as num?)?.toDouble(),
        atrasada: json['atrasada'] == true,
      );
}
