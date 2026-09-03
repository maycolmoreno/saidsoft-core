import '../../../core/network/api_client.dart';
import 'notificacion_model.dart';

class NotificacionesRepository {
  const NotificacionesRepository(this._apiClient);

  final ApiClient _apiClient;

  Future<List<Notificacion>> listar() async {
    final data = await _apiClient.get('/notificaciones');
    return (data as List)
        .map((item) => Notificacion.fromJson(Map<String, dynamic>.from(item as Map)))
        .toList();
  }

  Future<int> obtenerConteo() async {
    final data = await _apiClient.get('/notificaciones/count');
    if (data is Map) {
      final count = data['count'];
      if (count is int) {
        return count;
      }
      if (count is num) {
        return count.toInt();
      }
      return int.tryParse(count?.toString() ?? '') ?? 0;
    }
    if (data is int) {
      return data;
    }
    if (data is num) {
      return data.toInt();
    }
    return int.tryParse(data?.toString() ?? '') ?? 0;
  }

  Future<void> marcarLeida(int notificacionId) async {
    await _apiClient.post('/notificaciones/$notificacionId/leer', {});
  }
}
