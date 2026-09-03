import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:image/image.dart' as img;

import 'package:cresio_campo/nucleo/imagen/marca_agua.dart';

/// La marca es evidencia: tiene que quedar SIEMPRE, y cuando no hay ubicación tiene
/// que decirlo en vez de omitirse — una foto sin sello se confunde con una que nunca
/// se estampó.
void main() {
  late Directory temporal;

  setUp(() => temporal = Directory.systemTemp.createTempSync('marca_test'));
  tearDown(() => temporal.deleteSync(recursive: true));

  /// Crea una foto blanca: sobre blanco el texto solo sería ilegible, así que sirve
  /// para comprobar que la banda oscura se dibuja.
  File fotoBlanca({int ancho = 800, int alto = 600}) {
    final imagen = img.Image(width: ancho, height: alto);
    img.fill(imagen, color: img.ColorRgb8(255, 255, 255));
    final archivo = File('${temporal.path}/foto.jpg')
      ..writeAsBytesSync(img.encodeJpg(imagen));
    return archivo;
  }

  group('texto de la marca', () {
    test('con ubicacion muestra fecha y coordenadas', () {
      final lineas = DatosMarca(
        rutaOrigen: 'x',
        latitud: -3.2889458,
        longitud: -79.8829064,
        precisionMetros: 12.4,
        momento: DateTime(2026, 9, 3, 14, 5, 30),
      ).lineas;

      expect(lineas.first, '03/09/2026 14:05:30');
      expect(lineas[1], contains('-3.288946'));
      expect(lineas[1], contains('-79.882906'));
      expect(lineas[1], contains('12 m'));
    });

    test('sin ubicacion lo dice explicitamente', () {
      final lineas = DatosMarca(
        rutaOrigen: 'x',
        latitud: null,
        longitud: null,
        precisionMetros: null,
        momento: DateTime(2026, 9, 3, 14, 5, 30),
      ).lineas;

      expect(lineas.first, '03/09/2026 14:05:30');
      expect(lineas[1], 'Ubicacion no disponible');
    });

    test('sin precision no inventa un margen de error', () {
      final lineas = DatosMarca(
        rutaOrigen: 'x', latitud: -3.0, longitud: -79.0,
        precisionMetros: null, momento: DateTime(2026, 9, 3),
      ).lineas;
      expect(lineas[1], isNot(contains('+/-')));
    });
  });

  group('estampado', () {
    test('devuelve una imagen valida con la banda oscura dibujada', () async {
      final origen = fotoBlanca();
      final resultado = await estampar(DatosMarca(
        rutaOrigen: origen.path,
        latitud: -3.2889458, longitud: -79.8829064,
        precisionMetros: 10, momento: DateTime(2026, 9, 3, 14, 0),
      ));

      final imagen = img.decodeImage(resultado.readAsBytesSync());
      expect(imagen, isNotNull);

      // La franja del pie tiene que haber dejado de ser blanca.
      final pie = imagen!.getPixel(20, imagen.height - 20);
      expect(pie.r, lessThan(200), reason: 'el pie deberia estar oscurecido');
    });

    test('una foto grande se reduce para que el sello quede legible', () async {
      // La fuente es un mapa de bits de tamano fijo: en una foto de 3000 px el texto
      // seria diminuto.
      final origen = fotoBlanca(ancho: 3000, alto: 2000);
      final resultado = await estampar(DatosMarca(
        rutaOrigen: origen.path, latitud: -3.0, longitud: -79.0,
        precisionMetros: null, momento: DateTime(2026, 9, 3),
      ));

      final imagen = img.decodeImage(resultado.readAsBytesSync())!;
      expect(imagen.width, 1280);
    });

    test('una foto chica no se agranda', () async {
      final origen = fotoBlanca(ancho: 640, alto: 480);
      final resultado = await estampar(DatosMarca(
        rutaOrigen: origen.path, latitud: -3.0, longitud: -79.0,
        precisionMetros: null, momento: DateTime(2026, 9, 3),
      ));

      final imagen = img.decodeImage(resultado.readAsBytesSync())!;
      expect(imagen.width, 640);
    });

    test('si el archivo no se puede leer devuelve el original, no falla', () async {
      // Perder la evidencia por no poder dibujar un texto encima seria peor que
      // quedarse sin la marca.
      final inexistente = '${temporal.path}/no-existe.jpg';
      final resultado = await estampar(DatosMarca(
        rutaOrigen: inexistente, latitud: -3.0, longitud: -79.0,
        precisionMetros: null, momento: DateTime(2026, 9, 3),
      ));
      expect(resultado.path, inexistente);
    });
  });
}
