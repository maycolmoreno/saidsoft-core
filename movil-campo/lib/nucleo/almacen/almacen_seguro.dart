import 'dart:convert';

import 'package:flutter_secure_storage/flutter_secure_storage.dart';

/// Sesión del técnico en el almacén cifrado del sistema.
///
/// Se guarda el TOKEN, nunca la contraseña: el servidor puede revocar un token, y
/// un teléfono perdido no entrega la clave del técnico.
class AlmacenSeguro {
  AlmacenSeguro({FlutterSecureStorage? almacen})
      : _almacen = almacen ?? const FlutterSecureStorage();

  final FlutterSecureStorage _almacen;

  static const _kToken = 'sesion_token';
  static const _kUsuario = 'sesion_usuario';
  static const _kNombre = 'sesion_nombre';
  static const _kId = 'sesion_id';
  static const _kPermisos = 'sesion_permisos';
  static const _kEsStaff = 'sesion_es_staff';
  static const _kBloqueo = 'sesion_bloqueo_biometrico';

  Future<void> guardarSesion({
    required String token,
    required String usuario,
    required String nombre,
    required int? id,
    required List<String> permisos,
    required bool esStaff,
  }) async {
    await _almacen.write(key: _kToken, value: token);
    await _almacen.write(key: _kUsuario, value: usuario);
    await _almacen.write(key: _kNombre, value: nombre);
    await _almacen.write(key: _kId, value: id?.toString());
    await _almacen.write(key: _kPermisos, value: jsonEncode(permisos));
    await _almacen.write(key: _kEsStaff, value: esStaff ? '1' : '0');
  }

  Future<String?> leerToken() => _almacen.read(key: _kToken);
  Future<String?> leerUsuario() => _almacen.read(key: _kUsuario);
  Future<String?> leerNombre() => _almacen.read(key: _kNombre);
  Future<bool> leerEsStaff() async => (await _almacen.read(key: _kEsStaff)) == '1';

  /// Si el técnico activó la cerradura biométrica. Vive en el almacén cifrado y no
  /// en SharedPreferences porque decide si el token se entrega o no: en preferencias
  /// claras, cualquier app con root podría apagarla sin tocar la sesión.
  Future<bool> leerBloqueoBiometrico() async =>
      (await _almacen.read(key: _kBloqueo)) == '1';

  Future<void> guardarBloqueoBiometrico(bool activo) =>
      _almacen.write(key: _kBloqueo, value: activo ? '1' : '0');

  Future<int?> leerId() async {
    final crudo = await _almacen.read(key: _kId);
    return crudo == null ? null : int.tryParse(crudo);
  }

  Future<List<String>> leerPermisos() async {
    final crudo = await _almacen.read(key: _kPermisos);
    if (crudo == null || crudo.isEmpty) return const [];
    try {
      final decodificado = jsonDecode(crudo);
      if (decodificado is List) {
        return decodificado.map((p) => p.toString()).toList();
      }
    } catch (_) {
      // Valor corrupto: se trata como sin permisos en vez de romper el arranque.
    }
    return const [];
  }

  /// Cerrar sesión borra TAMBIÉN la preferencia de bloqueo: es de la persona, no del
  /// teléfono. Si quedara, el próximo técnico que entre en ese mismo celular heredaría
  /// una cerradura que él no eligió y que se abre con la huella del anterior.
  Future<void> borrarSesion() async {
    for (final clave in [_kToken, _kUsuario, _kNombre, _kId, _kPermisos, _kEsStaff, _kBloqueo]) {
      await _almacen.delete(key: clave);
    }
  }
}
