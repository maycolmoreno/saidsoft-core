import 'package:intl/intl.dart';

/// Cómo va el mantenimiento contra su acuerdo de nivel de servicio. Lo calcula el
/// backend (Mantenimiento.estado_sla) para que la app y el panel coincidan siempre.
enum EstadoSla { enPlazo, porVencer, incumplido, cumplido, sinSla }

EstadoSla _slaDesde(String? valor) => switch (valor) {
      'en_plazo' => EstadoSla.enPlazo,
      'por_vencer' => EstadoSla.porVencer,
      'incumplido' => EstadoSla.incumplido,
      'cumplido' => EstadoSla.cumplido,
      _ => EstadoSla.sinSla,
    };

class Farmacia {
  const Farmacia({
    required this.codigo,
    required this.nombre,
    required this.direccion,
    required this.latitud,
    required this.longitud,
  });

  final String codigo;
  final String nombre;
  final String direccion;
  final double? latitud;
  final double? longitud;

  bool get tieneCoordenadas => latitud != null && longitud != null;

  static Farmacia? desdeJson(dynamic json) {
    if (json is! Map) return null;
    return Farmacia(
      codigo: json['codigo']?.toString() ?? '',
      nombre: json['nombre']?.toString() ?? '',
      direccion: json['direccion']?.toString() ?? '',
      latitud: (json['latitud'] as num?)?.toDouble(),
      longitud: (json['longitud'] as num?)?.toDouble(),
    );
  }
}

class Equipo {
  const Equipo({
    required this.id,
    required this.codigo,
    required this.modelo,
    required this.numeroSerie,
    this.farmacia,
    this.custodio = '',
  });

  final int id;
  final String codigo;
  final String modelo;
  final String numeroSerie;

  /// Dónde está y quién lo usa. Vienen al buscar, para que el técnico distinga dos
  /// equipos del mismo modelo sin abrir cada uno.
  final Farmacia? farmacia;
  final String custodio;

  /// Segunda línea del resultado de búsqueda: lo que permite reconocerlo.
  String get detalle => [
        if (modelo.isNotEmpty) modelo,
        if (numeroSerie.isNotEmpty) numeroSerie,
        if (farmacia != null) farmacia!.codigo,
        if (custodio.isNotEmpty) custodio,
      ].join(' · ');

  factory Equipo.desdeJson(Map<String, dynamic> json) => Equipo(
        id: json['id'] as int? ?? 0,
        codigo: json['codigo']?.toString() ?? '',
        modelo: json['modelo']?.toString() ?? '',
        numeroSerie: json['numero_serie']?.toString() ?? '',
        farmacia: Farmacia.desdeJson(json['farmacia']),
        custodio: json['custodio']?.toString() ?? '',
      );
}

class Mantenimiento {
  const Mantenimiento({
    required this.id,
    required this.descripcion,
    required this.estado,
    required this.prioridad,
    required this.estadoSla,
    required this.limiteResolucion,
    required this.fechaProgramada,
    required this.equipos,
    required this.farmacia,
    required this.resultadoTecnico,
  });

  final int id;
  final String descripcion;
  final String estado;
  final String prioridad;
  final EstadoSla estadoSla;
  final DateTime? limiteResolucion;
  final DateTime? fechaProgramada;
  final List<Equipo> equipos;
  final Farmacia? farmacia;
  final String resultadoTecnico;

  bool get pendiente => estado == 'pendiente';
  bool get enProceso => estado == 'en_proceso';
  bool get cerrado => estado == 'cerrado';
  bool get abierto => pendiente || enProceso;

  Equipo? get equipoPrincipal => equipos.isEmpty ? null : equipos.first;

  String get etiquetaEstado => switch (estado) {
        'pendiente' => 'Pendiente',
        'en_proceso' => 'En proceso',
        'cerrado' => 'Cerrado',
        'cancelado' => 'Cancelado',
        _ => estado,
      };

  String get etiquetaPrioridad => switch (prioridad) {
        'critica' => 'Critica',
        'alta' => 'Alta',
        'normal' => 'Normal',
        'baja' => 'Baja',
        _ => prioridad,
      };

  /// Cuánto queda (o hace cuánto se pasó) del límite de resolución, en texto corto
  /// para el listado. El técnico prioriza con esto.
  String get restanteSla {
    final limite = limiteResolucion;
    if (limite == null) return '';
    final diferencia = limite.difference(DateTime.now());
    final vencido = diferencia.isNegative;
    final d = diferencia.abs();
    final texto = d.inDays >= 1
        ? '${d.inDays} d'
        : d.inHours >= 1
            ? '${d.inHours} h'
            : '${d.inMinutes} min';
    return vencido ? 'vencido hace $texto' : 'quedan $texto';
  }

  String get fechaLegible {
    final fecha = fechaProgramada;
    if (fecha == null) return '';
    return DateFormat('dd/MM/yyyy HH:mm').format(fecha.toLocal());
  }

  factory Mantenimiento.desdeJson(Map<String, dynamic> json) => Mantenimiento(
        id: json['id'] as int? ?? 0,
        descripcion: json['descripcion']?.toString() ?? '',
        estado: json['estado_interno']?.toString() ?? '',
        prioridad: json['prioridad']?.toString() ?? '',
        estadoSla: _slaDesde(json['estado_sla']?.toString()),
        limiteResolucion: DateTime.tryParse(json['limite_resolucion']?.toString() ?? ''),
        fechaProgramada: DateTime.tryParse(json['fecha_programada']?.toString() ?? ''),
        equipos: (json['equipos'] as List? ?? const [])
            .map((e) => Equipo.desdeJson(Map<String, dynamic>.from(e as Map)))
            .toList(),
        farmacia: Farmacia.desdeJson(json['farmacia']),
        resultadoTecnico: json['resultado_tecnico']?.toString() ?? '',
      );
}

/// Ítem del checklist con su marca para un mantenimiento concreto.
class ItemChecklist {
  const ItemChecklist({
    required this.id,
    required this.nombre,
    required this.realizada,
  });

  final int id;
  final String nombre;
  final bool realizada;

  ItemChecklist copiarCon({bool? realizada}) => ItemChecklist(
        id: id,
        nombre: nombre,
        realizada: realizada ?? this.realizada,
      );

  factory ItemChecklist.desdeJson(Map<String, dynamic> json) => ItemChecklist(
        id: json['id'] as int? ?? 0,
        nombre: json['nombre']?.toString() ?? '',
        realizada: json['realizada'] == true,
      );
}

/// Catálogo de resultados de cierre. Se mantiene en la app y no se pide por API para
/// que el cierre funcione SIN CONEXIÓN, que es justo cuando el técnico está en la
/// farmacia. Debe seguir a ResultadoTecnico del backend.
const resultadosTecnicos = <String, String>{
  'reparado': 'Reparado',
  'sin_falla': 'Sin falla encontrada',
  'sin_intervencion': 'Sin intervencion',
  'parcialmente_reparado': 'Parcialmente reparado',
  'requiere_repuesto': 'Requiere repuesto',
  'escalado_a_proveedor': 'Escalado a proveedor',
  'irreparable': 'Irreparable',
  'requiere_baja': 'Requiere baja',
  'garantia_aplicada': 'Garantia aplicada',
  'garantia_rechazada': 'Garantia rechazada',
  'actualizado': 'Actualizado',
  'instalado': 'Instalado',
};

const estadosGenerales = <String, String>{
  'operativo': 'Operativo',
  'requiere_revision': 'Requiere revision',
  'no_operativo': 'No operativo',
};
