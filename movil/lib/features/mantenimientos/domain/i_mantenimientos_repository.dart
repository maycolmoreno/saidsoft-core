import '../../../core/models/pagina_response.dart';

/// Contrato del repositorio de mantenimientos.
/// Permite desacoplar providers y servicios de la implementación concreta
/// (ApiClient, LocalDatabase, etc.).
abstract class IMantenimientosRepository {
  Future<List<Map<String, dynamic>>> listar();
  Future<PaginaResponse<Map<String, dynamic>>> listarPaginado(
      {int page = 0, int size = 20});
  Future<Map<String, dynamic>> obtenerDetalle(int mantenimientoId);
  Future<List<Map<String, dynamic>>> listarCustodios();
  Future<List<Map<String, dynamic>>> listarCustodias();
  /// Catálogo global de actividades, para armar el checklist al crear (cuando
  /// todavía no hay un mantenimiento contra el que consultarlo).
  Future<List<Map<String, dynamic>>> listarActividadesChecklist();

  /// Checklist de un mantenimiento concreto: cada ítem del catálogo con su marca
  /// de realizado.
  Future<List<Map<String, dynamic>>> listarChecklistDeMantenimiento(
      int mantenimientoId);

  Future<Map<String, dynamic>> crear({
    required int equipoId,
    required int custodioId,
    required String tipoMantenimiento,
    required String fechaMantenimiento,
    required String detalle,
    required String estadoGeneral,
    required List<Map<String, dynamic>> actividades,
    String? firmaTecnico,
    String? firmaCustodio,
  });

  Future<List<Map<String, dynamic>>> crearVarios({
    required List<int> equipoIds,
    required int custodioId,
    required String tipoMantenimiento,
    required String fechaMantenimiento,
    required String detalle,
    required String estadoGeneral,
    required List<Map<String, dynamic>> actividades,
    String? firmaTecnico,
    String? firmaCustodio,
  });

  /// Crea mantenimientos; si no hay red, encola para sync.
  /// Retorna `true` si fue encolado (offline), `false` si se creó online.
  Future<bool> crearVariosConFallback({
    required List<int> equipoIds,
    required int custodioId,
    required String tipoMantenimiento,
    required String fechaMantenimiento,
    required String detalle,
    required String estadoGeneral,
    required List<Map<String, dynamic>> actividades,
    required List<Map<String, dynamic>> imagenes,
    String? firmaTecnico,
    String? firmaCustodio,
  });

  Future<void> guardarImagenes({
    required int mantenimientoId,
    required List<Map<String, dynamic>> imagenes,
  });

  /// Cierra el mantenimiento.
  ///
  /// `resultadoTecnico` es obligatorio y sale del catálogo del backend (reparado,
  /// sin_falla, requiere_repuesto, ...). En InvTICS el cierre era solo texto libre;
  /// acá el resultado decide si el equipo vuelve a bodega y si se recomienda darlo
  /// de baja, así que la app tiene que preguntarlo en vez de inventarlo.
  Future<void> cerrar({
    required int mantenimientoId,
    required String resultadoTecnico,
    int? tiempoRealMinutos,
    String estadoGeneral,
  });

  /// Cierra mantenimiento; si no hay red, encola para sync.
  /// Retorna `true` si fue encolado (offline), `false` si se cerró online.
  Future<bool> cerrarConFallback({
    required int mantenimientoId,
    required String resultadoTecnico,
    int? tiempoRealMinutos,
    String estadoGeneral,
  });

  Future<void> syncQueuedCreate(Map<String, dynamic> payload);
  Future<void> syncQueuedClose(Map<String, dynamic> payload);
}
