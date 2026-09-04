import 'package:flutter/services.dart';
import 'package:local_auth/local_auth.dart';

/// Cerradura biométrica sobre la sesión ya guardada.
///
/// Importante para no prometer lo que no hace: la huella **no autentica contra el
/// servidor**. Android/iOS nunca entregan la huella a la app ni la mandan a ningún
/// lado — solo responden "sí, es la persona registrada en este teléfono". Lo que se
/// gana es una cerradura sobre el token que ya vive en el almacén cifrado: sin esto,
/// cualquiera que agarre el celular desbloqueado abre la app y actúa como el técnico
/// (crea mantenimientos, registra equipos, manda su ubicación), y el token de DRF no
/// vence nunca.
///
/// Por eso el primer ingreso SIEMPRE es con usuario y clave: es lo único que prueba
/// quién es contra el servidor. La huella solo evita repetirlo.
class BloqueoBiometrico {
  BloqueoBiometrico({LocalAuthentication? auth})
      : _auth = auth ?? LocalAuthentication();

  final LocalAuthentication _auth;

  /// True si el teléfono puede pedir huella/rostro **o** el PIN del sistema.
  ///
  /// Se acepta el PIN como respaldo a propósito: un lector sucio o un dedo mojado no
  /// pueden dejar al técnico afuera en medio de una farmacia.
  Future<bool> disponible() async {
    try {
      if (!await _auth.isDeviceSupported()) return false;
      return await _auth.canCheckBiometrics || await _auth.isDeviceSupported();
    } on PlatformException {
      return false;
    }
  }

  /// True si además hay al menos una huella/rostro ya registrado en el teléfono.
  ///
  /// Distinto de [disponible]: un equipo con lector pero sin huella registrada
  /// soporta la biometría y aun así no puede usarla, y ofrecerla ahí sería ofrecer un
  /// botón que no funciona.
  Future<bool> hayBiometriaRegistrada() async {
    try {
      return (await _auth.getAvailableBiometrics()).isNotEmpty;
    } on PlatformException {
      return false;
    }
  }

  /// Pide la huella. Devuelve true solo si el sistema confirmó la identidad.
  ///
  /// Nunca lanza: cualquier fallo se trata como "no autenticado" y la app cae al
  /// ingreso con usuario y clave, que siempre funciona.
  Future<bool> autenticar({
    String motivo = 'Confirmá tu identidad para entrar a SAIDSOFT Campo',
  }) async {
    try {
      return await _auth.authenticate(
        localizedReason: motivo,
        options: const AuthenticationOptions(
          // Permite caer al PIN/patrón del teléfono si la huella falla.
          biometricOnly: false,
          // La app queda bloqueada mientras el técnico atiende una llamada o mira una
          // foto: sin esto, salir de la app cancelaría el pedido y volvería al inicio.
          stickyAuth: true,
          useErrorDialogs: true,
        ),
      );
    } on PlatformException {
      // Sin hardware, sin huella registrada, o el usuario canceló.
      return false;
    }
  }
}
