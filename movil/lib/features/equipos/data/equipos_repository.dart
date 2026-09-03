import '../../../core/models/pagina_response.dart';
import '../../../core/network/api_client.dart';
import '../domain/i_equipos_repository.dart';
import 'equipo_models.dart';

class EquiposRepository implements IEquiposRepository {
  const EquiposRepository(this._apiClient);

  final ApiClient _apiClient;

  @override
  Future<List<EquipoListItem>> listar() async {
    final data = await _apiClient.get('/equipos');
    final items = (data as List)
        .map((item) =>
            EquipoListItem.fromJson(Map<String, dynamic>.from(item as Map)))
        .toList();
    items.sort((a, b) => a.codigoSap.compareTo(b.codigoSap));
    return items;
  }

  @override
  Future<PaginaResponse<EquipoListItem>> listarPaginado(
      {int page = 0, int size = 20}) async {
    final data =
        await _apiClient.get('/equipos/paginado?page=$page&size=$size');
    return PaginaResponse.fromJson(
      Map<String, dynamic>.from(data as Map),
      (json) => EquipoListItem.fromJson(json),
    );
  }

  @override
  Future<EquipoHistorial> obtenerDetalle(int equipoId) async {
    final data = await _apiClient.get('/historial/$equipoId');
    return EquipoHistorial.fromJson(Map<String, dynamic>.from(data as Map));
  }

  @override
  Future<List<EquipoListItem>> listarConHistorial() async {
    final equipos = await listar();
    final enriched = <EquipoListItem>[];
    for (final equipo in equipos) {
      final equipoId = equipo.id;
      if (equipoId == 0) {
        enriched.add(equipo);
        continue;
      }
      try {
        final historial = await obtenerDetalle(equipoId);
        enriched.add(
          EquipoListItem(
            id: equipo.id,
            codigoSap: equipo.codigoSap,
            modelo: equipo.modelo,
            serial: equipo.serial,
            estadoEquipo: equipo.estadoEquipo,
            procesador: equipo.procesador,
            mac: equipo.mac,
            custodioNombre: historial.equipo.custodioNombre.isEmpty
                ? equipo.custodioNombre
                : historial.equipo.custodioNombre,
            ubicacionNombre: historial.equipo.ubicacionNombre.isEmpty
                ? equipo.ubicacionNombre
                : historial.equipo.ubicacionNombre,
            estadoMantenimiento: historial.estadoMantenimiento,
            diasSinMantenimiento: historial.estadisticas.diasSinMantenimiento,
          ),
        );
      } catch (_) {
        enriched.add(equipo);
      }
    }
    enriched.sort(_priorityCompare);
    return enriched;
  }
}

int _priorityCompare(EquipoListItem a, EquipoListItem b) {
  final rankCompare = _priorityRank(a).compareTo(_priorityRank(b));
  if (rankCompare != 0) {
    return rankCompare;
  }
  final diasA = a.diasSinMantenimiento;
  final diasB = b.diasSinMantenimiento;
  if (diasA != diasB) {
    return diasB.compareTo(diasA);
  }
  return a.codigoSap.compareTo(b.codigoSap);
}

int _priorityRank(EquipoListItem item) {
  final estadoEquipo = item.estadoEquipo.toUpperCase();
  final estadoMantenimiento = item.estadoMantenimiento.toUpperCase();
  final dias = item.diasSinMantenimiento;

  if (estadoEquipo == 'NO_OPERATIVO' || estadoEquipo == 'BAJA') {
    return 0;
  }
  if (estadoMantenimiento.contains('VENC') || dias >= 180) {
    return 1;
  }
  if (estadoMantenimiento.contains('PROX') || dias >= 90) {
    return 2;
  }
  if (estadoEquipo == 'REQUIERE_REVISION') {
    return 3;
  }
  return 4;
}
