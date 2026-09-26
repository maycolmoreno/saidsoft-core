import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';

import 'package:cresio_campo/nucleo/red/api.dart';

/// Un 409 con `codigo: conflicto_de_estado` significa "alguien mas ya movio esto".
///
/// Es lo que separa "reintentar cuando vuelva la senal" de "esto ya no se arregla
/// solo". Antes todo caia en el mismo 400 generico, el sincronizador lo reintentaba
/// para siempre y el tecnico veia un contador de pendientes que nunca bajaba.
void main() {
  String cuerpoDe({
    String detalle = 'Un mantenimiento cancelado no puede cerrarse.',
    String estado = 'cancelado',
    String? por = 'mesa.ayuda',
    String? cuando = '2026-09-26T14:00:00Z',
    String codigo = 'conflicto_de_estado',
  }) =>
      jsonEncode({
        'detail': detalle,
        'codigo': codigo,
        'estado_actual': estado,
        'modificado_por': por,
        'modificado_en': cuando,
      });

  group('se reconoce el conflicto', () {
    test('trae el mensaje, el estado y quien lo movio', () {
      final c = conflictoDesdeCuerpo(cuerpoDe())!;
      expect(c.mensaje, contains('cancelado'));
      expect(c.estadoActual, 'cancelado');
      expect(c.modificadoPor, 'mesa.ayuda');
      expect(c.modificadoEn, DateTime.parse('2026-09-26T14:00:00Z'));
    });

    test('es un ErrorApi pero NO un DatosRechazados', () {
      final c = conflictoDesdeCuerpo(cuerpoDe())!;
      expect(c, isA<ErrorApi>());
      expect(c, isNot(isA<DatosRechazados>()));
    });

    test('la explicacion dice quien fue y que el trabajo no se perdio', () {
      final c = conflictoDesdeCuerpo(cuerpoDe())!;
      expect(c.explicacion, contains('mesa.ayuda'));
      expect(c.explicacion, contains('NO se perdio'));
    });

    test('sin autor (las visitas no lo guardan) sigue siendo legible', () {
      final c = conflictoDesdeCuerpo(
        cuerpoDe(por: null, cuando: null, estado: 'realizada'),
      )!;
      expect(c.modificadoPor, isNull);
      expect(c.estadoActual, 'realizada');
      expect(c.explicacion, contains('NO se perdio'));
      expect(c.explicacion, isNot(contains('null')));
    });
  });

  group('lo que NO es un conflicto', () {
    test('un 409 sin nuestro codigo no se toma como conflicto', () {
      // Un intermediario puede devolver 409 por su cuenta. Tratarlo como conflicto
      // dejaria la accion sin reintentar por algo que quiza si se resuelve solo.
      expect(conflictoDesdeCuerpo('{"detail": "otra cosa"}'), isNull);
      expect(conflictoDesdeCuerpo(cuerpoDe(codigo: 'otro_codigo')), isNull);
    });

    test('un cuerpo que no es JSON no rompe', () {
      expect(conflictoDesdeCuerpo('<html>502 Bad Gateway</html>'), isNull);
      expect(conflictoDesdeCuerpo(''), isNull);
    });
  });
}
