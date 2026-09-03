import '../../../core/network/api_client.dart';
import 'planificacion_models.dart';

class PlanificacionRepository {
  const PlanificacionRepository(this._apiClient);

  final ApiClient _apiClient;

  Future<List<ActividadPlanificada>> listarTodas() async {
    final data = await _apiClient.get('/actividades-planificadas');
    return (data as List)
        .map((item) => ActividadPlanificada.fromJson(
            Map<String, dynamic>.from(item as Map)))
        .toList();
  }

  Future<List<ActividadPlanificada>> listarPorTecnico(int tecnicoId) async {
    final data =
        await _apiClient.get('/actividades-planificadas/tecnico/$tecnicoId');
    return (data as List)
        .map((item) => ActividadPlanificada.fromJson(
            Map<String, dynamic>.from(item as Map)))
        .toList();
  }

  Future<List<ActividadPlanificada>> listarPorTecnicoYEstado(
      int tecnicoId, String estado) async {
    final data = await _apiClient
        .get('/actividades-planificadas/tecnico/$tecnicoId/estado/$estado');
    return (data as List)
        .map((item) => ActividadPlanificada.fromJson(
            Map<String, dynamic>.from(item as Map)))
        .toList();
  }

  Future<ActividadPlanificada> crear(ActividadPlanificada actividad) async {
    final data =
        await _apiClient.post('/actividades-planificadas', actividad.toJson());
    return ActividadPlanificada.fromJson(
        Map<String, dynamic>.from(data as Map));
  }

  Future<ActividadPlanificada> cambiarEstado(
    int idActividad, {
    required String estado,
    int? tiempoRealMinutos,
    String? observaciones,
  }) async {
    final body = <String, dynamic>{
      'estado': estado,
      if (tiempoRealMinutos != null) 'tiempoRealMinutos': tiempoRealMinutos,
      if (observaciones != null) 'observaciones': observaciones,
    };
    final data = await _apiClient.put(
        '/actividades-planificadas/$idActividad/estado', body);
    return ActividadPlanificada.fromJson(
        Map<String, dynamic>.from(data as Map));
  }

  Future<MetricasCumplimiento> obtenerMetricasTecnico(
      int tecnicoId, String periodo) async {
    final data = await _apiClient.get(
        '/actividades-planificadas/metricas/tecnico/$tecnicoId?periodo=$periodo');
    return MetricasCumplimiento.fromJson(
        Map<String, dynamic>.from(data as Map));
  }
}
