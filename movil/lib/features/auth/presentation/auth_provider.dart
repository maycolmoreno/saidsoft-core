import 'package:flutter/foundation.dart';

import '../data/auth_models.dart';
import '../data/auth_repository.dart';

enum AuthStatus {
  loading,
  authenticated,
  unauthenticated,
  requiresServerConfig,
}

class AuthProvider extends ChangeNotifier {
  AuthProvider({required AuthRepository repository}) : _repository = repository;

  final AuthRepository _repository;

  AuthStatus _status = AuthStatus.loading;
  AuthStatus get status => _status;

  AuthSession? _session;
  AuthSession? get session => _session;
  bool get isAuthenticated => _session != null;
  String get roleLabel => _session?.roleLabel ?? 'Sin rol';
  int? get userId => _session?.userId;

  bool hasCapability(UserCapability capability) {
    return _session?.capabilities.has(capability) ?? false;
  }

  String? _errorMessage;
  String? get errorMessage => _errorMessage;

  Future<void> bootstrap({required bool hasServerConfig}) async {
    if (!hasServerConfig) {
      _status = AuthStatus.requiresServerConfig;
      notifyListeners();
      return;
    }

    final stored = await _repository.readStoredSession();
    if (stored == null) {
      _status = AuthStatus.unauthenticated;
      notifyListeners();
      return;
    }

    _session = stored;
    try {
      _session = await _repository.refreshSession(stored);
    } catch (_) {
      _session = stored;
    }
    _status = AuthStatus.authenticated;
    notifyListeners();
  }

  Future<bool> login(String username, String password) async {
    final trimmedUsername = username.trim();
    if (trimmedUsername.isEmpty || password.isEmpty) {
      _errorMessage = 'El usuario y la contraseña son obligatorios.';
      _status = AuthStatus.unauthenticated;
      notifyListeners();
      return false;
    }

    _errorMessage = null;
    _status = AuthStatus.loading;
    notifyListeners();

    try {
      final session = await _repository.login(
        LoginRequest(username: trimmedUsername, password: password),
      );
      _session = session;
      _status = AuthStatus.authenticated;
      notifyListeners();
      return true;
    } catch (e) {
      _status = AuthStatus.unauthenticated;
      _errorMessage = e.toString().replaceAll('Exception: ', '');
      notifyListeners();
      return false;
    }
  }

  Future<void> logout() async {
    await _repository.logout();
    _session = null;
    _status = AuthStatus.unauthenticated;
    _errorMessage = null;
    notifyListeners();
  }

  Future<void> refreshSession({bool silent = true}) async {
    final current = _session;
    if (current == null) {
      return;
    }
    try {
      final refreshed = await _repository.refreshSession(current);
      final changed = refreshed.role != current.role ||
          refreshed.userId != current.userId ||
          !_sameModules(refreshed.modules, current.modules) ||
          refreshed.displayName != current.displayName ||
          refreshed.username != current.username;
      _session = refreshed;
      if (changed || !silent) {
        notifyListeners();
      }
    } catch (e) {
      if (!silent) {
        _errorMessage = e.toString().replaceAll('Exception: ', '');
        notifyListeners();
      }
    }
  }

  void markServerConfigured() {
    _status = AuthStatus.unauthenticated;
    _errorMessage = null;
    notifyListeners();
  }

  bool _sameModules(List<String> a, List<String> b) {
    if (a.length != b.length) {
      return false;
    }
    final left = a.map((item) => item.trim().toUpperCase()).toSet();
    final right = b.map((item) => item.trim().toUpperCase()).toSet();
    if (left.length != right.length) {
      return false;
    }
    return left.containsAll(right);
  }
}
