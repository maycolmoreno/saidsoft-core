import 'package:mockito/annotations.dart';
import 'package:http/http.dart' as http;
import 'package:cresio_mobile/core/network/api_client.dart';
import 'package:cresio_mobile/core/storage/secure_storage_service.dart';

@GenerateMocks([
  http.Client,
  SecureStorageService,
  ApiClient,
])
void main() {}
