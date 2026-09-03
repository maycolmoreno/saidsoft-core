import 'dart:convert';

import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:mockito/mockito.dart';

import 'package:cresio_mobile/core/errors/exceptions.dart';
import 'package:cresio_mobile/core/network/api_client.dart';
import '../../helpers/test_helpers.mocks.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  late MockClient mockClient;
  late MockSecureStorageService mockStorage;
  late ApiClient apiClient;
  late bool onUnauthorizedCalled;

  setUp(() {
    // Stub connectivity_plus platform channel to return wifi.
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(
      const MethodChannel('dev.fluttercommunity.plus/connectivity'),
      (MethodCall call) async {
        if (call.method == 'check') return ['wifi'];
        return null;
      },
    );

    mockClient = MockClient();
    mockStorage = MockSecureStorageService();
    onUnauthorizedCalled = false;

    apiClient = ApiClient(
      client: mockClient,
      secureStorage: mockStorage,
      onUnauthorized: () async {
        onUnauthorizedCalled = true;
      },
    );

    // Default: no token stored.
    when(mockStorage.readToken()).thenAnswer((_) async => null);
    when(mockStorage.clearSession()).thenAnswer((_) async {});
  });

  // ──────────────────────── GET ────────────────────────

  group('get', () {
    test('returns parsed JSON on 200', () async {
      when(mockStorage.readToken()).thenAnswer((_) async => 'dGVzdDp0ZXN0');
      when(mockClient.get(any, headers: anyNamed('headers'))).thenAnswer(
        (_) async => http.Response(jsonEncode({'id': 1, 'nombre': 'PC'}), 200),
      );

      final result = await apiClient.get('/equipos/1');

      expect(result, isA<Map>());
      expect(result['id'], 1);
    });

    test('returns null on 204 empty body', () async {
      when(mockClient.get(any, headers: anyNamed('headers')))
          .thenAnswer((_) async => http.Response('', 204));

      final result = await apiClient.get('/equipos');

      expect(result, isNull);
    });

    test('throws NotFoundException on 404', () async {
      when(mockClient.get(any, headers: anyNamed('headers')))
          .thenAnswer((_) async => http.Response('', 404));

      expect(() => apiClient.get('/equipos/999'),
          throwsA(isA<NotFoundException>()));
    });

    test('throws ServerException on 500', () async {
      when(mockClient.get(any, headers: anyNamed('headers')))
          .thenAnswer((_) async => http.Response('Internal Server Error', 500));

      expect(() => apiClient.get('/equipos'), throwsA(isA<ServerException>()));
    });

    test('throws AuthException on 401 for auth paths', () async {
      when(mockClient.get(any, headers: anyNamed('headers')))
          .thenAnswer((_) async => http.Response('', 401));

      expect(() => apiClient.get('/auth/yo'), throwsA(isA<AuthException>()));
    });
  });

  // ──────────────────────── POST ────────────────────────

  group('post', () {
    test('sends body and returns parsed JSON on 200', () async {
      when(mockStorage.readToken()).thenAnswer((_) async => 'dGVzdDp0ZXN0');
      when(mockClient.post(any,
              headers: anyNamed('headers'), body: anyNamed('body')))
          .thenAnswer(
        (_) async => http.Response(jsonEncode({'id': 5}), 201),
      );

      final result = await apiClient.post('/equipos', {'nombre': 'Laptop'});

      expect(result['id'], 5);
      final captured = verify(mockClient.post(any,
              headers: anyNamed('headers'), body: captureAnyNamed('body')))
          .captured
          .single as String;
      expect(jsonDecode(captured)['nombre'], 'Laptop');
    });
  });

  // ──────────────────────── Headers ────────────────────────

  group('headers', () {
    test('includes Authorization when token exists', () async {
      when(mockStorage.readToken()).thenAnswer((_) async => 'abc123');
      when(mockClient.get(any, headers: anyNamed('headers')))
          .thenAnswer((_) async => http.Response('{}', 200));

      await apiClient.get('/test');

      final headers =
          verify(mockClient.get(any, headers: captureAnyNamed('headers')))
              .captured
              .single as Map<String, String>;
      // "Token", no "Basic": el backend Django usa TokenAuthentication de DRF.
      expect(headers['Authorization'], 'Token abc123');
    });

    test('excludes Authorization when no token', () async {
      when(mockStorage.readToken()).thenAnswer((_) async => null);
      when(mockClient.get(any, headers: anyNamed('headers')))
          .thenAnswer((_) async => http.Response('{}', 200));

      await apiClient.get('/test');

      final headers =
          verify(mockClient.get(any, headers: captureAnyNamed('headers')))
              .captured
              .single as Map<String, String>;
      expect(headers.containsKey('Authorization'), isFalse);
    });
  });

  // ──────────────────────── 401 + Unauthorised Handler ────────────────────────

  group('401 handling', () {
    test('clears session and calls onUnauthorized on auth path 401', () async {
      when(mockClient.get(any, headers: anyNamed('headers')))
          .thenAnswer((_) async => http.Response('', 401));

      try {
        await apiClient.get('/auth/yo');
      } on AuthException {
        // expected
      }

      verify(mockStorage.clearSession()).called(1);
      expect(onUnauthorizedCalled, isTrue);
    });
  });
}
