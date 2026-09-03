import 'package:flutter/foundation.dart';

import '../../../core/network/api_client.dart';
import '../data/planificacion_models.dart';
import '../data/planificacion_repository.dart';

class PlanificacionProvider extends ChangeNotifier {
  PlanificacionProvider(this._apiClient) {
    _repository = PlanificacionRepository(_apiClient);
  }

  final ApiClient _apiClient;
  late final PlanificacionRepository _repository;

  List<ActividadPlanificada> _actividades = const [];
  MetricasCumplimiento? _metricas;
  bool _loading = false;
  String? _error;
  String _filtroEstado = '';
  String _periodoMetricas = 'MENSUAL';

  List<ActividadPlanificada> get actividades {
    if (_filtroEstado.isEmpty) return _actividades;
    return _actividades.where((a) => a.estado == _filtroEstado).toList();
  }

  List<ActividadPlanificada> get todasActividades => _actividades;
  MetricasCumplimiento? get metricas => _metricas;
  bool get loading => _loading;
  String? get error => _error;
  String get filtroEstado => _filtroEstado;
  String get periodoMetricas => _periodoMetricas;

  int get totalPendientes => _actividades.where((a) => a.isPendiente).length;
  int get totalEnProgreso => _actividades.where((a) => a.isEnProgreso).length;
  int get totalCompletadas => _actividades.where((a) => a.isCompletada).length;
  int get totalVencidas => _actividades.where((a) => a.isVencida).length;

  void setFiltroEstado(String estado) {
    _filtroEstado = estado;
    notifyListeners();
  }

  Future<void> cargarActividades({int? tecnicoId}) async {
    _loading = true;
    _error = null;
    notifyListeners();

    try {
      if (tecnicoId != null) {
        _actividades = await _repository.listarPorTecnico(tecnicoId);
      } else {
        _actividades = await _repository.listarTodas();
      }
    } catch (e) {
      _error = e.toString().replaceAll('Exception: ', '');
    }
    _loading = false;
    notifyListeners();
  }

  Future<void> cargarMetricas(int tecnicoId) async {
    _loading = true;
    _error = null;
    notifyListeners();

    try {
      _metricas =
          await _repository.obtenerMetricasTecnico(tecnicoId, _periodoMetricas);
    } catch (e) {
      _error = e.toString().replaceAll('Exception: ', '');
    }
    _loading = false;
    notifyListeners();
  }

  void setPeriodoMetricas(String periodo) {
    _periodoMetricas = periodo;
    notifyListeners();
  }

  Future<bool> crearActividad(ActividadPlanificada actividad) async {
    try {
      await _repository.crear(actividad);
      await cargarActividades(tecnicoId: actividad.tecnicoId);
      return true;
    } catch (e) {
      _error = e.toString().replaceAll('Exception: ', '');
      notifyListeners();
      return false;
    }
  }

  Future<bool> cambiarEstado(
    int idActividad, {
    required String estado,
    int? tiempoRealMinutos,
    String? observaciones,
  }) async {
    try {
      await _repository.cambiarEstado(
        idActividad,
        estado: estado,
        tiempoRealMinutos: tiempoRealMinutos,
        observaciones: observaciones,
      );
      // Refresh current list
      await cargarActividades();
      return true;
    } catch (e) {
      _error = e.toString().replaceAll('Exception: ', '');
      notifyListeners();
      return false;
    }
  }
}
