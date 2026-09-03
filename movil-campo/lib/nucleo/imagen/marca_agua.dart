import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:image/image.dart' as img;
import 'package:intl/intl.dart';

/// Datos que se estampan sobre la foto.
///
/// Es una clase y no parámetros sueltos porque tiene que viajar a otro isolate
/// (ver [estampar]), y ahí solo pasan valores simples.
@immutable
class DatosMarca {
  const DatosMarca({
    required this.rutaOrigen,
    required this.latitud,
    required this.longitud,
    required this.precisionMetros,
    required this.momento,
  });

  final String rutaOrigen;

  /// null cuando no se pudo obtener la posición. La marca lo dice explícitamente en
  /// vez de omitirse: una foto sin sello se confunde con una que nunca se estampó.
  final double? latitud;
  final double? longitud;
  final double? precisionMetros;
  final DateTime momento;

  List<String> get lineas {
    final fecha = DateFormat('dd/MM/yyyy HH:mm:ss').format(momento);
    if (latitud == null || longitud == null) {
      return [fecha, 'Ubicacion no disponible'];
    }
    final precision =
        precisionMetros == null ? '' : '  (+/- ${precisionMetros!.round()} m)';
    return [
      fecha,
      'Lat ${latitud!.toStringAsFixed(6)}  Lon ${longitud!.toStringAsFixed(6)}$precision',
    ];
  }
}

/// Estampa fecha y coordenadas sobre la foto y devuelve el archivo resultante.
///
/// Corre en otro isolate: decodificar y recodificar un JPEG de varios megapíxeles en
/// el hilo de interfaz congela la app justo después de la cámara.
///
/// Nunca lanza: si algo falla se devuelve la foto ORIGINAL. Perder la evidencia por
/// no poder dibujar un texto encima sería peor que quedarse sin la marca.
Future<File> estampar(DatosMarca datos) async {
  try {
    final resultado = await compute(_estamparSync, datos);
    if (resultado == null) return File(datos.rutaOrigen);
    final destino = File('${datos.rutaOrigen}_marcada.jpg');
    await destino.writeAsBytes(resultado);
    return destino;
  } catch (_) {
    return File(datos.rutaOrigen);
  }
}

/// Parte pesada, en un isolate aparte. Devuelve null si no se pudo procesar.
Uint8List? _estamparSync(DatosMarca datos) {
  final bytes = File(datos.rutaOrigen).readAsBytesSync();
  final foto = img.decodeImage(bytes);
  if (foto == null) return null;

  // La fuente es de tamaño fijo (mapa de bits), así que en una foto grande queda
  // diminuta. Se dibuja sobre una copia reducida a un ancho conocido para que el
  // sello tenga siempre una proporción legible, y de paso baja el peso de la subida.
  const anchoObjetivo = 1280;
  final lienzo = foto.width > anchoObjetivo
      ? img.copyResize(foto, width: anchoObjetivo)
      : foto;

  final fuente = img.arial24;
  final lineas = datos.lineas;
  const margen = 12;
  final altoTexto = fuente.lineHeight * lineas.length + margen;

  // Banda oscura detrás del texto: sobre una foto clara (una pantalla encendida, una
  // pared blanca) el texto solo sería ilegible.
  img.fillRect(
    lienzo,
    x1: 0,
    y1: lienzo.height - altoTexto - margen,
    x2: lienzo.width,
    y2: lienzo.height,
    color: img.ColorRgba8(0, 0, 0, 160),
  );

  var y = lienzo.height - altoTexto;
  for (final linea in lineas) {
    img.drawString(
      lienzo,
      linea,
      font: fuente,
      x: margen,
      y: y,
      color: img.ColorRgb8(255, 255, 255),
    );
    y += fuente.lineHeight;
  }

  return img.encodeJpg(lienzo, quality: 85);
}
