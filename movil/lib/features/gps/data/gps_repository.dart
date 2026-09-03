import '../../../core/network/api_client.dart';
import 'gps_models.dart';

class GpsRepository {
  GpsRepository({required ApiClient apiClient}) : _apiClient = apiClient;

  final ApiClient _apiClient;

  Future<void> enviarUbicacion(UbicacionTecnicoRequest request) async {
    await _apiClient.post('/ubicaciones-tecnicos', request.toJson());
  }

  Future<void> registrarConsentimiento(ConsentimientoRequest request) async {
    await _apiClient.post(
      '/ubicaciones-tecnicos/consentimiento',
      request.toJson(),
    );
  }

  Future<List<UbicacionActivaResponse>> obtenerUbicacionesTiempoReal() async {
    final data = await _apiClient.get('/ubicaciones-tecnicos/tiempo-real');
    if (data is! List) {
      throw Exception('Respuesta inesperada del servidor');
    }
    return data
        .cast<Map<String, dynamic>>()
        .map(UbicacionActivaResponse.fromJson)
        .toList();
  }
}
