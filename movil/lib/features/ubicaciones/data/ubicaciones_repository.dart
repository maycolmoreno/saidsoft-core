import '../../../core/network/api_client.dart';
import 'ubicacion_model.dart';

class UbicacionesRepository {
  const UbicacionesRepository(this._apiClient);

  final ApiClient _apiClient;

  Future<List<Ubicacion>> listar() async {
    final data = await _apiClient.get('/ubicaciones');
    final items = (data as List)
        .map((item) => Ubicacion.fromJson(Map<String, dynamic>.from(item as Map)))
        .toList();
    items.sort((a, b) => a.nombre.compareTo(b.nombre));
    return items;
  }

  Future<Ubicacion> crear({
    required String nombre,
    required String agencia,
    String? ciudad,
    String? direccion,
  }) async {
    final data = await _apiClient.post('/ubicaciones', {
      'nombre': nombre,
      'agencia': agencia,
      'ciudad': ciudad,
      'direccion': direccion,
      'estado': true,
    });
    return Ubicacion.fromJson(Map<String, dynamic>.from(data as Map));
  }

  Future<Ubicacion> actualizar({
    required int idUbicacion,
    required String nombre,
    required String agencia,
    String? ciudad,
    String? direccion,
    required bool estado,
  }) async {
    final data = await _apiClient.put('/ubicaciones/$idUbicacion', {
      'idUbicacion': idUbicacion,
      'nombre': nombre,
      'agencia': agencia,
      'ciudad': ciudad,
      'direccion': direccion,
      'estado': estado,
    });
    return Ubicacion.fromJson(Map<String, dynamic>.from(data as Map));
  }

  Future<void> actualizarEstado({
    required int idUbicacion,
    required bool estado,
  }) async {
    await _apiClient.put('/ubicaciones/estado/$idUbicacion', {'estado': estado});
  }
}
