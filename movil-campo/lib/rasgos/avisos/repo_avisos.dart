import '../../nucleo/red/api.dart';

class Aviso {
  const Aviso({
    required this.id,
    required this.mensaje,
    required this.leida,
    required this.mantenimientoId,
    required this.creadoEn,
  });

  final int id;
  final String mensaje;
  final bool leida;
  final int? mantenimientoId;
  final DateTime? creadoEn;

  factory Aviso.desdeJson(Map<String, dynamic> json) => Aviso(
        id: json['id'] as int? ?? 0,
        mensaje: json['mensaje']?.toString() ?? '',
        leida: json['leida'] == true,
        mantenimientoId: json['mantenimiento'] as int?,
        creadoEn: DateTime.tryParse(json['creado_en']?.toString() ?? ''),
      );
}

/// Bandeja de avisos del técnico: asignaciones nuevas y vencimientos.
class RepoAvisos {
  const RepoAvisos(this._api);
  final Api _api;

  Future<int> sinLeer() async {
    final datos = await _api.obtener('/notificaciones/count/');
    if (datos is Map && datos['count'] is int) return datos['count'] as int;
    return 0;
  }

  Future<List<Aviso>> listar() async {
    final datos = await _api.obtener('/notificaciones/') as List;
    return datos
        .map((a) => Aviso.desdeJson(Map<String, dynamic>.from(a as Map)))
        .toList();
  }

  Future<void> marcarLeido(int id) => _api.publicar('/notificaciones/$id/leer/');
}
