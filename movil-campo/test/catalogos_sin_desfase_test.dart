import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

import 'package:cresio_campo/rasgos/mantenimientos/mantenimiento.dart';

/// Los catalogos que la app tiene COMPILADOS no pueden desviarse del backend.
///
/// Estan compilados a proposito: el cierre de un mantenimiento tiene que funcionar en
/// una farmacia sin senal, y pedirlos por API lo haria imposible. El costo de esa
/// decision es que pueden desincronizarse en silencio -- nada falla, simplemente la app
/// ofrece una opcion que el servidor rechaza, o deja de ofrecer una que existe.
///
/// `test/datos/catalogos_produccion.json` es una respuesta REAL de `/api/v1/catalogos/`.
/// Si el backend cambia EstadoGeneralEquipo, hay que refrescar ese archivo y esta
/// prueba avisa que el `const` quedo viejo.
///
/// Se comparan las CLAVES y no las etiquetas: la clave es lo que viaja al servidor, y
/// las etiquetas difieren por acentos en toda la app a proposito.
void main() {
  Map<String, dynamic> catalogosDeProduccion() {
    final crudo = File('test/datos/catalogos_produccion.json').readAsStringSync();
    return Map<String, dynamic>.from(jsonDecode(crudo) as Map);
  }

  Set<String> clavesDe(String catalogo) => ((catalogosDeProduccion()[catalogo] as List))
      .map((o) => (o as Map)['valor'].toString())
      .toSet();

  test('estadosGenerales coincide con EstadoGeneralEquipo del backend', () {
    expect(estadosGenerales.keys.toSet(), clavesDe('estados_generales'));
  });

  test('el catalogo compilado no ofrece nada que el servidor vaya a rechazar', () {
    // La direccion que rompe en campo: el tecnico elige una opcion y el cierre vuelve
    // con un 400 -- que ademas, viniendo de la cola offline, llega horas despues,
    // cuando ya se fue de la farmacia.
    final delBackend = clavesDe('estados_generales');
    for (final clave in estadosGenerales.keys) {
      expect(delBackend, contains(clave), reason: '"$clave" no existe en el backend');
    }
  });

  test('ninguna etiqueta quedo vacia', () {
    for (final entrada in estadosGenerales.entries) {
      expect(entrada.value.trim(), isNotEmpty, reason: '${entrada.key} sin etiqueta');
    }
  });
}
