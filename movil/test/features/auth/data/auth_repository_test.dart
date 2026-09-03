import 'package:flutter_test/flutter_test.dart';
import 'package:mockito/mockito.dart';

import 'package:cresio_mobile/features/auth/data/auth_models.dart';
import 'package:cresio_mobile/features/auth/data/auth_repository.dart';
import '../../../helpers/test_helpers.mocks.dart';

void main() {
  late MockSecureStorageService mockStorage;
  late AuthRepository repository;

  setUp(() {
    mockStorage = MockSecureStorageService();
    repository = AuthRepository(secureStorage: mockStorage);
  });

  // ──────────────────────── logout ────────────────────────

  group('logout', () {
    test('clears the stored session', () async {
      when(mockStorage.clearSession()).thenAnswer((_) async {});

      await repository.logout();

      verify(mockStorage.clearSession()).called(1);
    });
  });

  // ──────────────────────── readStoredSession ────────────────────────

  group('readStoredSession', () {
    test('returns null when no token is stored', () async {
      when(mockStorage.readToken()).thenAnswer((_) async => null);

      final session = await repository.readStoredSession();

      expect(session, isNull);
    });

    test('returns null when token is empty', () async {
      when(mockStorage.readToken()).thenAnswer((_) async => '');

      final session = await repository.readStoredSession();

      expect(session, isNull);
    });

    test('returns AuthSession when token exists', () async {
      when(mockStorage.readToken()).thenAnswer((_) async => 'dGVzdDp0ZXN0');
      when(mockStorage.readUsername())
          .thenAnswer((_) async => 'admin@test.com');
      when(mockStorage.readDisplayName()).thenAnswer((_) async => 'Admin');
      when(mockStorage.readRole()).thenAnswer((_) async => 'ADMIN');
      when(mockStorage.readUserId()).thenAnswer((_) async => '42');
      // readStoredSession también lee los permisos guardados; sin estos stubs el
      // mock lanzaba MissingStubError (venía fallando así desde InvTICS).
      when(mockStorage.readModules()).thenAnswer((_) async => const <String>[]);
      when(mockStorage.readModulesLoaded()).thenAnswer((_) async => false);

      final session = await repository.readStoredSession();

      expect(session, isNotNull);
      expect(session, isA<AuthSession>());
      expect(session!.token, 'dGVzdDp0ZXN0');
      expect(session.username, 'admin@test.com');
      expect(session.displayName, 'Admin');
      expect(session.role, 'ADMIN');
      expect(session.userId, 42);
    });

    test('uses username as displayName when displayName is null', () async {
      when(mockStorage.readToken()).thenAnswer((_) async => 'tok');
      when(mockStorage.readUsername()).thenAnswer((_) async => 'user@test.com');
      when(mockStorage.readDisplayName()).thenAnswer((_) async => null);
      when(mockStorage.readRole()).thenAnswer((_) async => null);
      when(mockStorage.readUserId()).thenAnswer((_) async => null);
      when(mockStorage.readModules()).thenAnswer((_) async => const <String>[]);
      when(mockStorage.readModulesLoaded()).thenAnswer((_) async => false);

      final session = await repository.readStoredSession();

      expect(session!.displayName, 'user@test.com');
      expect(session.role, '');
      expect(session.userId, isNull);
    });
  });
}
