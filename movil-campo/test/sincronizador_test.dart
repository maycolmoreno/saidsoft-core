import 'package:flutter_test/flutter_test.dart';

import 'package:cresio_campo/nucleo/almacen/cola_offline.dart';
import 'package:cresio_campo/nucleo/almacen/sincronizador.dart';

/// Lo que se sube cuando una accion estuvo encolada sin senal.
///
/// El punto de todo esto: el backend fechaba la accion CUANDO LLEGABA, no cuando el
/// tecnico la hizo. Un cierre a las 10:00 en una farmacia sin senal, sincronizado a
/// las 18:00, quedaba a las 18:00 -- y de ahi salian un SLA incumplido que no fue y
/// una ventana de verificacion GPS corrida ocho horas.
void main() {
  final hechaALas10 = DateTime.utc(2026, 9, 26, 15, 0);

  AccionPendiente accion(String tipo, Map<String, dynamic> datos) => AccionPendiente(
        id: 1,
        tipo: tipo,
        datos: datos,
        creadaEn: hechaALas10,
        intentos: 0,
        ultimoError: '',
      );

  group('hora real de la accion', () {
    test('el cierre viaja con la hora en que se hizo, no con la de ahora', () {
      final destino = destinoDeAccion(accion(ColaOffline.tipoCerrar, {
        'mantenimiento_id': 7,
        'resultado_tecnico': 'reparado',
        'tiempo_real_minutos': 45,
      }))!;

      expect(destino.ruta, '/mantenimientos/7/cerrar/');
      expect(destino.cuerpo['ocurrido_en'], hechaALas10.toIso8601String());
      // El resto del cuerpo sigue igual, y `mantenimiento_id` no se manda: va en la URL.
      expect(destino.cuerpo['resultado_tecnico'], 'reparado');
      expect(destino.cuerpo['tiempo_real_minutos'], 45);
      expect(destino.cuerpo.containsKey('mantenimiento_id'), isFalse);
    });

    test('la llegada al mantenimiento viaja con su hora', () {
      final destino = destinoDeAccion(accion(ColaOffline.tipoIniciar, {'id': 3}))!;
      expect(destino.ruta, '/mantenimientos/3/iniciar/');
      expect(destino.cuerpo['ocurrido_en'], hechaALas10.toIso8601String());
    });

    test('la firma viaja con la hora en que firmo el custodio', () {
      final destino = destinoDeAccion(accion(ColaOffline.tipoFirmar, {
        'mantenimiento_id': 9,
        'tipo_firma': 'custodio',
        'firma_base64': 'xx',
      }))!;
      expect(destino.ruta, '/mantenimientos/9/firmar/');
      expect(destino.cuerpo['ocurrido_en'], hechaALas10.toIso8601String());
      expect(destino.cuerpo['tipo_firma'], 'custodio');
    });

    test('la visita viaja con su hora, en llegada y en cierre', () {
      final llegada = destinoDeAccion(accion(ColaOffline.tipoIniciarVisita, {'id': 4}))!;
      expect(llegada.ruta, '/visitas/4/iniciar/');
      expect(llegada.cuerpo['ocurrido_en'], hechaALas10.toIso8601String());

      final cierre = destinoDeAccion(
        accion(ColaOffline.tipoCerrarVisita, {'id': 4, 'observaciones': 'todo ok'}),
      )!;
      expect(cierre.ruta, '/visitas/4/cerrar/');
      expect(cierre.cuerpo['ocurrido_en'], hechaALas10.toIso8601String());
      expect(cierre.cuerpo['observaciones'], 'todo ok');
    });

    test('la hora va en UTC, que es lo que el backend parsea', () {
      final local = DateTime(2026, 9, 26, 10, 0);
      final destino = destinoDeAccion(AccionPendiente(
        id: 1,
        tipo: ColaOffline.tipoIniciar,
        datos: const {'id': 1},
        creadaEn: local,
        intentos: 0,
        ultimoError: '',
      ))!;
      expect(destino.cuerpo['ocurrido_en'], local.toUtc().toIso8601String());
      expect(destino.cuerpo['ocurrido_en'].toString(), endsWith('Z'));
    });
  });

  group('clave de idempotencia', () {
    test('toda accion viaja con el id de su fila en la cola', () {
      // Si el servidor ya proceso la accion pero la respuesta se perdio, el reintento
      // llega con el MISMO origen_id y el backend devuelve la respuesta original en vez
      // de duplicar el hecho o rebotar con un error que la dejaria trabada para siempre.
      final casos = <String, Map<String, dynamic>>{
        ColaOffline.tipoIniciar: {'id': 1},
        ColaOffline.tipoChecklist: {'mantenimiento_id': 1, 'actividad_id': 2, 'realizada': true},
        ColaOffline.tipoFirmar: {'mantenimiento_id': 1, 'tipo_firma': 'tecnico', 'firma_base64': 'x'},
        ColaOffline.tipoCerrar: {'mantenimiento_id': 1, 'resultado_tecnico': 'reparado'},
        ColaOffline.tipoIniciarVisita: {'id': 1},
        ColaOffline.tipoCerrarVisita: {'id': 1, 'observaciones': ''},
        ColaOffline.tipoUbicacion: {'latitud': -2.17, 'longitud': -79.92},
      };
      casos.forEach((tipo, datos) {
        final destino = destinoDeAccion(AccionPendiente(
          id: 42,
          tipo: tipo,
          datos: datos,
          creadaEn: hechaALas10,
          intentos: 0,
          ultimoError: '',
        ))!;
        expect(destino.cuerpo['origen_id'], 42, reason: '$tipo deberia mandar origen_id');
      });
    });

    test('el origen_id es el de la fila, no el del mantenimiento', () {
      final destino = destinoDeAccion(AccionPendiente(
        id: 99,
        tipo: ColaOffline.tipoCerrar,
        datos: const {'mantenimiento_id': 7, 'resultado_tecnico': 'reparado'},
        creadaEn: hechaALas10,
        intentos: 0,
        ultimoError: '',
      ))!;
      expect(destino.cuerpo['origen_id'], 99);
    });
  });

  group('lo que NO cambia', () {
    test('el checklist no lleva hora: es un estado, no un instante', () {
      final destino = destinoDeAccion(accion(ColaOffline.tipoChecklist, {
        'mantenimiento_id': 2,
        'actividad_id': 5,
        'realizada': true,
      }))!;
      expect(destino.cuerpo.containsKey('ocurrido_en'), isFalse);
      expect(destino.cuerpo['actividad_id'], 5);
    });

    test('la ubicacion conserva su propio timestamp_captura', () {
      final destino = destinoDeAccion(accion(ColaOffline.tipoUbicacion, {
        'latitud': -2.17,
        'longitud': -79.92,
        'timestamp_captura': '2026-09-26T15:00:00.000Z',
      }))!;
      expect(destino.ruta, '/ubicaciones-tecnico/');
      expect(destino.cuerpo['timestamp_captura'], '2026-09-26T15:00:00.000Z');
    });

    test('un tipo desconocido no se manda a ningun lado', () {
      expect(destinoDeAccion(accion('invento_de_una_version_futura', const {})), isNull);
    });
  });
}
