import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';

import 'package:cresio_campo/nucleo/red/api.dart';

/// Un 403 con `codigo: sin_consentimiento` no es un 403 por permisos.
///
/// Lo que corresponde hacer es distinto: una posicion rechazada porque la persona
/// retiro (o nunca dio) el consentimiento de monitoreo **se descarta**, no se reintenta
/// ni se conserva. Reintentarla no va a funcionar nunca, y dejarla en la cola del
/// telefono seria guardar justo el dato que pidio no registrar.
void main() {
  group('se reconoce la falta de consentimiento', () {
    test('con el codigo del backend, si', () {
      final cuerpo = jsonEncode({
        'detail': 'No hay un consentimiento de monitoreo vigente para registrar tu ubicacion.',
        'codigo': 'sin_consentimiento',
      });
      expect(esFaltaDeConsentimiento(cuerpo), isTrue);
    });

    test('un 403 comun de permisos, no', () {
      // Sin el codigo se trata como falta de permisos: el caso conservador, porque
      // conserva la accion en vez de tirarla.
      expect(esFaltaDeConsentimiento('{"detail": "No tenes permiso para esta accion."}'), isFalse);
      expect(esFaltaDeConsentimiento(jsonEncode({'codigo': 'otro_codigo'})), isFalse);
    });

    test('un cuerpo que no es JSON no rompe ni tira la accion', () {
      expect(esFaltaDeConsentimiento('<html>403 Forbidden</html>'), isFalse);
      expect(esFaltaDeConsentimiento(''), isFalse);
    });
  });

  group('son tipos distintos porque piden acciones distintas', () {
    test('las dos son ErrorApi pero ninguna es la otra', () {
      const sinConsentimiento = SinConsentimiento();
      const sinPermiso = SinPermiso();
      expect(sinConsentimiento, isA<ErrorApi>());
      expect(sinPermiso, isA<ErrorApi>());
      expect(sinConsentimiento, isNot(isA<SinPermiso>()));
      expect(sinPermiso, isNot(isA<SinConsentimiento>()));
    });

    test('cada una explica lo suyo', () {
      expect(const SinConsentimiento().mensaje, contains('consentimiento'));
      expect(const SinPermiso().mensaje, contains('permiso'));
    });
  });
}
