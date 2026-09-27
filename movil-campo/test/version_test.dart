import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

import 'package:cresio_campo/nucleo/version.dart';

/// El aviso de version nueva, y el guard que evita que mienta.
///
/// La distribucion del APK es manual: alguien lo instala telefono por telefono. Sin
/// aviso, un tecnico puede pasar semanas con una version vieja sin ninguna senal de que
/// existe otra -- que con distribucion a mano es lo normal, no la excepcion.
void main() {
  group('buildActual no puede quedar viejo', () {
    test('coincide con el +N de pubspec.yaml', () {
      // `buildActual` esta escrito a mano para no sumar un plugin nativo por un entero.
      // El costo es que puede desviarse, y entonces la app compararia contra un numero
      // que no es el suyo: o avisa de una version que ya tiene, o no avisa de una que
      // le falta. Esto lo vuelve imposible de pasar por alto.
      final pubspec = File('pubspec.yaml').readAsStringSync();
      final encontrado =
          RegExp(r'^version:\s*[0-9.]+\+(\d+)\s*$', multiLine: true).firstMatch(pubspec);
      expect(encontrado, isNotNull, reason: 'pubspec.yaml sin `version: X.Y.Z+N`');
      expect(
        int.parse(encontrado!.group(1)!),
        buildActual,
        reason: 'buildActual (version.dart) quedo desfasado del +N de pubspec.yaml',
      );
    });
  });

  group('cuando avisar', () {
    VersionPublicada conBuild(int build) => VersionPublicada(
          version: '9.9.9',
          build: build,
          notas: '',
          url: 'http://servidor/media/movil/x.apk',
        );

    test('avisa solo si el build publicado es MAYOR', () {
      expect(conBuild(buildActual + 1).hayQueActualizar, isTrue);
      expect(conBuild(buildActual).hayQueActualizar, isFalse);
      // Mas viejo que el instalado: pasa si alguien republica una version anterior.
      // No avisar es lo correcto -- sugerir "actualizar" hacia atras seria peor.
      expect(conBuild(buildActual - 1).hayQueActualizar, isFalse);
    });
  });

  group('respuestas del servidor', () {
    test('sin ninguna version publicada, no hay nada que avisar', () {
      expect(VersionPublicada.desdeJson({'publicada': false}), isNull);
    });

    test('una respuesta completa se lee entera', () {
      final v = VersionPublicada.desdeJson({
        'publicada': true,
        'version': '1.10.0',
        'build': 13,
        'notas': 'Hora real de la cola offline',
        'url': 'http://10.111.6.20:8080/media/movil/saidsoft-campo-1.10.0+13.apk',
      })!;
      expect(v.version, '1.10.0');
      expect(v.build, 13);
      expect(v.notas, 'Hora real de la cola offline');
      expect(v.url, endsWith('.apk'));
    });

    test('un cuerpo inesperado no rompe: devuelve null', () {
      // Un servidor viejo que no tiene el endpoint, o una respuesta rara. El aviso es
      // una conveniencia: que falle no puede estorbarle el trabajo al tecnico.
      expect(VersionPublicada.desdeJson(null), isNull);
      expect(VersionPublicada.desdeJson('no soy json'), isNull);
      expect(VersionPublicada.desdeJson({}), isNull);
    });

    test('un build que viene como texto se interpreta igual', () {
      final v = VersionPublicada.desdeJson({
        'publicada': true,
        'version': '1.10.0',
        'build': '13',
        'url': '',
      })!;
      expect(v.build, 13);
    });
  });
}
