import 'dart:io';
import 'dart:math';

import '../../../core/errors/exceptions.dart';
import '../../../core/models/pagina_response.dart';
import '../../../core/network/api_client.dart';
import '../../../core/storage/local_database.dart';
import '../domain/i_mantenimientos_repository.dart';

class MantenimientosRepository implements IMantenimientosRepository {
  const MantenimientosRepository(this._apiClient);

  final ApiClient _apiClient;

  @override
  Future<List<Map<String, dynamic>>> listar() async {
    final data = await _apiClient.get('/mantenimiento');
    return (data as List)
        .map((item) => Map<String, dynamic>.from(item as Map))
        .toList();
  }

  @override
  Future<PaginaResponse<Map<String, dynamic>>> listarPaginado(
      {int page = 0, int size = 20}) async {
    final data =
        await _apiClient.get('/mantenimiento/paginado?page=$page&size=$size');
    return PaginaResponse.fromJson(
      Map<String, dynamic>.from(data as Map),
      (json) => json,
    );
  }

  @override
  Future<Map<String, dynamic>> obtenerDetalle(int mantenimientoId) async {
    final data = await _apiClient.get('/mantenimiento/$mantenimientoId');
    return Map<String, dynamic>.from(data as Map);
  }

  @override
  Future<List<Map<String, dynamic>>> listarCustodios() async {
    final data = await _apiClient.get('/custodios');
    final items = (data as List)
        .map((item) => Map<String, dynamic>.from(item as Map))
        .toList();
    items.sort((a, b) => _text(a['nombre']).compareTo(_text(b['nombre'])));
    return items;
  }

  @override
  Future<List<Map<String, dynamic>>> listarCustodias() async {
    final data = await _apiClient.get('/custodias');
    return (data as List)
        .map((item) => Map<String, dynamic>.from(item as Map))
        .toList();
  }

  @override
  Future<List<Map<String, dynamic>>> listarActividadesChecklist() async {
    final data = await _apiClient.get('/actividades-checklist');
    final items = (data as List)
        .map((item) => Map<String, dynamic>.from(item as Map))
        .toList();
    items.sort((a, b) {
      final categoryCompare =
          _primeraCategoria(a).compareTo(_primeraCategoria(b));
      if (categoryCompare != 0) {
        return categoryCompare;
      }
      return _asInt(a['orden']).compareTo(_asInt(b['orden']));
    });
    return items;
  }

  @override
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
  }) async {
    final data = await _apiClient.post('/mantenimiento', {
      'equipoId': equipoId,
      'custodioId': custodioId,
      'tipoMantenimiento': tipoMantenimiento,
      'fechaMantenimiento': fechaMantenimiento,
      'detalle': detalle,
      'estadoGeneral': estadoGeneral,
      'firmaTecnico': firmaTecnico,
      'firmaCustodio': firmaCustodio,
      'actividades': actividades,
      'imagenes': <Map<String, dynamic>>[],
    });
    return Map<String, dynamic>.from(data as Map);
  }

  @override
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
  }) async {
    final data = await _apiClient.post('/mantenimiento', {
      'equipoIds': equipoIds,
      'custodioId': custodioId,
      'tipoMantenimiento': tipoMantenimiento,
      'fechaMantenimiento': fechaMantenimiento,
      'detalle': detalle,
      'estadoGeneral': estadoGeneral,
      'firmaTecnico': firmaTecnico,
      'firmaCustodio': firmaCustodio,
      'actividades': actividades,
      'imagenes': <Map<String, dynamic>>[],
    });
    return [Map<String, dynamic>.from(data as Map)];
  }

  @override
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
  }) async {
    try {
      final createdItems = await crearVarios(
        equipoIds: equipoIds,
        custodioId: custodioId,
        tipoMantenimiento: tipoMantenimiento,
        fechaMantenimiento: fechaMantenimiento,
        detalle: detalle,
        estadoGeneral: estadoGeneral,
        actividades: actividades,
        firmaTecnico: firmaTecnico,
        firmaCustodio: firmaCustodio,
      );
      if (imagenes.isNotEmpty) {
        for (final created in createdItems) {
          final mantenimientoId = _asInt(created['idMantenimiento']) == 0
              ? _asInt(created['id'])
              : _asInt(created['idMantenimiento']);
          if (mantenimientoId > 0) {
            await guardarImagenes(
              mantenimientoId: mantenimientoId,
              imagenes: imagenes,
            );
          }
        }
      }
      return false;
    } on OfflineException {
      await LocalDatabase.instance.enqueueSyncOperation(
        id: _syncId('create'),
        operation: 'create_mantenimiento',
        payload: {
          'equipoIds': equipoIds,
          'custodioId': custodioId,
          'tipoMantenimiento': tipoMantenimiento,
          'fechaMantenimiento': fechaMantenimiento,
          'detalle': detalle,
          'estadoGeneral': estadoGeneral,
          'actividades': actividades,
          'imagenes': imagenes,
          'firmaTecnico': firmaTecnico,
          'firmaCustodio': firmaCustodio,
        },
      );
      return true;
    }
  }

  @override
  Future<void> guardarImagenes({
    required int mantenimientoId,
    required List<Map<String, dynamic>> imagenes,
  }) async {
    final files = imagenes
        .map((img) => img['rutaArchivo'] as String?)
        .where((path) => path != null && path.isNotEmpty)
        .map((path) => File(path!))
        .where((file) => file.existsSync())
        .toList();
    if (files.isNotEmpty) {
      await _apiClient.postMultipart(
        '/mantenimiento/$mantenimientoId/imagenes/upload',
        files,
        <String, String>{},
      );
    }
  }

  @override
  Future<void> cerrar({
    required int mantenimientoId,
    required String observaciones,
  }) async {
    await _apiClient.post('/mantenimiento/cerrar/$mantenimientoId', {
      'descripcionTrabajoRealizado': observaciones,
    });
  }

  @override
  Future<bool> cerrarConFallback({
    required int mantenimientoId,
    required String observaciones,
  }) async {
    try {
      await cerrar(
        mantenimientoId: mantenimientoId,
        observaciones: observaciones,
      );
      return false;
    } on OfflineException {
      await LocalDatabase.instance.enqueueSyncOperation(
        id: _syncId('close'),
        operation: 'close_mantenimiento',
        payload: {
          'mantenimientoId': mantenimientoId,
          'observaciones': observaciones,
        },
      );
      return true;
    }
  }

  @override
  Future<void> syncQueuedCreate(Map<String, dynamic> payload) async {
    final equipoIds = (payload['equipoIds'] as List? ?? const [])
        .map((item) => _asInt(item))
        .where((item) => item > 0)
        .toList();
    if (equipoIds.isEmpty) {
      throw const ServerException(
          'No hay equipos pendientes para sincronizar.');
    }
    final actividades = (payload['actividades'] as List? ?? const [])
        .map((item) => Map<String, dynamic>.from(item as Map))
        .toList();
    final imagenes = (payload['imagenes'] as List? ?? const [])
        .map((item) => Map<String, dynamic>.from(item as Map))
        .toList();
    final createdItems = await crearVarios(
      equipoIds: equipoIds,
      custodioId: _asInt(payload['custodioId']),
      tipoMantenimiento: _text(payload['tipoMantenimiento']),
      fechaMantenimiento: _text(payload['fechaMantenimiento']),
      detalle: _text(payload['detalle']),
      estadoGeneral: _text(payload['estadoGeneral']),
      actividades: actividades,
      firmaTecnico: _text(payload['firmaTecnico'], fallback: ''),
      firmaCustodio: _text(payload['firmaCustodio'], fallback: ''),
    );
    if (imagenes.isNotEmpty) {
      for (final created in createdItems) {
        final mantenimientoId = _asInt(created['idMantenimiento']) == 0
            ? _asInt(created['id'])
            : _asInt(created['idMantenimiento']);
        if (mantenimientoId > 0) {
          await guardarImagenes(
            mantenimientoId: mantenimientoId,
            imagenes: imagenes,
          );
        }
      }
    }
  }

  @override
  Future<void> syncQueuedClose(Map<String, dynamic> payload) async {
    await cerrar(
      mantenimientoId: _asInt(payload['mantenimientoId']),
      observaciones: _text(payload['observaciones']),
    );
  }
}

String _text(dynamic value, {String fallback = ''}) {
  final text = value?.toString().trim() ?? '';
  return text.isEmpty ? fallback : text;
}

/// Extrae la primera categoría de la lista `categorias` del JSON del backend.
String _primeraCategoria(Map<String, dynamic> item, {String fallback = ''}) {
  final categorias = item['categorias'];
  if (categorias is List && categorias.isNotEmpty) {
    return _text(categorias.first, fallback: fallback);
  }
  // Retrocompatibilidad: si el backend aún devuelve 'categoria' (String)
  return _text(item['categoria'], fallback: fallback);
}

int _asInt(dynamic value) {
  if (value is int) {
    return value;
  }
  if (value is num) {
    return value.toInt();
  }
  return int.tryParse(value?.toString() ?? '') ?? 0;
}

String _syncId(String prefix) {
  final random = Random().nextInt(1 << 32).toRadixString(16);
  return '$prefix-${DateTime.now().microsecondsSinceEpoch}-$random';
}
