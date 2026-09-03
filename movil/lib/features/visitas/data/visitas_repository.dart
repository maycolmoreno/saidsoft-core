import '../../../core/network/api_client.dart';

class VisitasRepository {
  const VisitasRepository(this._apiClient);

  final ApiClient _apiClient;

  Future<List<Map<String, dynamic>>> listarUbicaciones() async {
    final data = await _apiClient.get('/ubicaciones');
    return (data as List)
        .map((item) => Map<String, dynamic>.from(item as Map))
        .toList();
  }

  Future<List<Map<String, dynamic>>> listarCustodios({int? ubicacionId}) async {
    final suffix = ubicacionId == null ? '' : '?ubicacionId=$ubicacionId';
    final data = await _apiClient.get('/visita/custodios$suffix');
    return (data as List)
        .map((item) => Map<String, dynamic>.from(item as Map))
        .toList();
  }

  Future<List<Map<String, dynamic>>> listarEquipos({
    int? ubicacionId,
    int? custodioId,
  }) async {
    final query = <String>[
      if (ubicacionId != null) 'ubicacionId=$ubicacionId',
      if (custodioId != null) 'custodioId=$custodioId',
    ].join('&');
    final suffix = query.isEmpty ? '' : '?$query';
    final data = await _apiClient.get('/visita/equipos$suffix');
    return (data as List)
        .map((item) => Map<String, dynamic>.from(item as Map))
        .toList();
  }
}
