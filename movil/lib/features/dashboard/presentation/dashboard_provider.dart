import 'package:connectivity_plus/connectivity_plus.dart';
import 'package:flutter/foundation.dart';

import '../../../core/network/api_client.dart';
import '../../../core/storage/local_database.dart';
import '../../mantenimientos/domain/i_mantenimientos_repository.dart';
import '../../notificaciones/data/notificaciones_repository.dart';
import '../../sync/sync_service.dart';

class DashboardProvider extends ChangeNotifier {
  DashboardProvider(
    this._apiClient, {
    required IMantenimientosRepository mantenimientosRepository,
    required NotificacionesRepository notificacionesRepository,
  })  : _mantenimientosRepo = mantenimientosRepository,
        _notificacionesRepo = notificacionesRepository {
    _init();
  }

  final ApiClient _apiClient;
  final IMantenimientosRepository _mantenimientosRepo;
  final NotificacionesRepository _notificacionesRepo;

  bool _offline = false;
  int _pendingOffline = 0;
  int _pendingNotifications = 0;
  int _openMantenimientos = 0;
  int _activeEquipos = 0;
  String? _error;
  List<Map<String, dynamic>> _recentMantenimientos = const [];

  bool get offline => _offline;
  int get pendingOffline => _pendingOffline;
  int get pendingNotifications => _pendingNotifications;
  int get openMantenimientos => _openMantenimientos;
  int get activeEquipos => _activeEquipos;
  String? get error => _error;
  List<Map<String, dynamic>> get recentMantenimientos => _recentMantenimientos;

  Future<void> _init() async {
    await refresh();
  }

  Future<void> refresh() async {
    _error = null;
    final connectivity = await Connectivity().checkConnectivity();
    _offline = connectivity.contains(ConnectivityResult.none);
    _pendingOffline = await LocalDatabase.instance.contarPendientes();

    if (_offline) {
      _pendingNotifications = 0;
      _openMantenimientos = 0;
      _activeEquipos = 0;
      _recentMantenimientos = const [];
      notifyListeners();
      return;
    }

    try {
      await SyncService(_apiClient).syncPendingOperations();
      _pendingOffline = await LocalDatabase.instance.contarPendientes();

      // Llamadas paralelas: equipos, mantenimientos y notificaciones son independientes
      final results = await Future.wait([
        _apiClient.get('/equipos'),
        _mantenimientosRepo.listar(),
        _notificacionesRepo.obtenerConteo(),
      ]);

      final equipos = (results[0] as List)
          .map((item) => Map<String, dynamic>.from(item as Map))
          .toList();
      final mantenimientos = results[1] as List<Map<String, dynamic>>;
      _pendingNotifications = results[2] as int;

      _openMantenimientos = mantenimientos
          .where(
              (item) => _text(item['estadoInterno']).toUpperCase() != 'CERRADO')
          .length;
      _activeEquipos = equipos
          .where((item) => _text(item['estadoEquipo']).toUpperCase() != 'BAJA')
          .length;

      final recent = List<Map<String, dynamic>>.from(mantenimientos)
        ..sort(
          (a, b) => _text(b['fechaMantenimiento'])
              .compareTo(_text(a['fechaMantenimiento'])),
        );
      _recentMantenimientos = recent.take(4).toList();
    } catch (e) {
      _error = e.toString().replaceAll('Exception: ', '');
    }
    notifyListeners();
  }
}

String _text(dynamic value, {String fallback = ''}) {
  final text = value?.toString().trim() ?? '';
  return text.isEmpty ? fallback : text;
}
