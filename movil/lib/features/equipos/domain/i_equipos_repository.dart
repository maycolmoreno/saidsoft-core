import '../../../core/models/pagina_response.dart';
import '../data/equipo_models.dart';

/// Contrato del repositorio de equipos.
/// Las capas superiores (providers, use cases) dependen de esta abstracción
/// y no de la implementación concreta.
abstract class IEquiposRepository {
  Future<List<EquipoListItem>> listar();
  Future<PaginaResponse<EquipoListItem>> listarPaginado(
      {int page = 0, int size = 20});
  Future<EquipoHistorial> obtenerDetalle(int equipoId);
  Future<List<EquipoListItem>> listarConHistorial();
}
