import 'dart:convert';

import 'package:flutter_secure_storage/flutter_secure_storage.dart';

class SecureStorageService {
  static const _jwtKey = 'auth_jwt';
  static const _nameKey = 'auth_name';
  static const _roleKey = 'auth_role';
  static const _userKey = 'auth_user';
  static const _userIdKey = 'auth_user_id';
  static const _modulesKey = 'auth_modules';
  static const _modulesLoadedKey = 'auth_modules_loaded';

  final FlutterSecureStorage _storage;

  SecureStorageService({FlutterSecureStorage? storage})
      : _storage = storage ?? const FlutterSecureStorage();

  Future<void> saveSession({
    required String token,
    required String username,
    required String displayName,
    required String role,
    String? userId,
    List<String> modules = const [],
    bool modulesLoaded = false,
  }) async {
    await _storage.write(key: _jwtKey, value: token);
    await _storage.write(key: _userKey, value: username);
    await _storage.write(key: _nameKey, value: displayName);
    await _storage.write(key: _roleKey, value: role);
    await _storage.write(key: _modulesKey, value: jsonEncode(modules));
    await _storage.write(
      key: _modulesLoadedKey,
      value: modulesLoaded ? 'true' : 'false',
    );
    if (userId != null) {
      await _storage.write(key: _userIdKey, value: userId);
    }
  }

  Future<String?> readToken() => _storage.read(key: _jwtKey);
  Future<String?> readUsername() => _storage.read(key: _userKey);
  Future<String?> readDisplayName() => _storage.read(key: _nameKey);
  Future<String?> readRole() => _storage.read(key: _roleKey);
  Future<String?> readUserId() => _storage.read(key: _userIdKey);
  Future<bool> readModulesLoaded() async =>
      (await _storage.read(key: _modulesLoadedKey)) == 'true';
  Future<List<String>> readModules() async {
    final raw = await _storage.read(key: _modulesKey);
    if (raw == null || raw.isEmpty) {
      return const [];
    }
    try {
      final decoded = jsonDecode(raw);
      if (decoded is List) {
        return decoded
            .map((item) => item?.toString().trim() ?? '')
            .where((item) => item.isNotEmpty)
            .toList();
      }
    } catch (_) {
      return raw
          .split(',')
          .map((item) => item.trim())
          .where((item) => item.isNotEmpty)
          .toList();
    }
    return const [];
  }

  Future<void> clearSession() async {
    await _storage.delete(key: _jwtKey);
    await _storage.delete(key: _userKey);
    await _storage.delete(key: _nameKey);
    await _storage.delete(key: _roleKey);
    await _storage.delete(key: _userIdKey);
    await _storage.delete(key: _modulesKey);
    await _storage.delete(key: _modulesLoadedKey);
  }
}
