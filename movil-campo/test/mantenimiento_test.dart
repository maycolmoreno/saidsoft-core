import 'package:flutter_test/flutter_test.dart';

import 'package:cresio_campo/rasgos/mantenimientos/mantenimiento.dart';

/// El modelo traduce lo que manda DRF. Un campo mal leído acá no rompe la app: la
/// deja mostrando "-" o comparando contra algo que nunca coincide, que es peor
/// porque parece que funciona.
void main() {
  Map<String, dynamic> json({
    String estado = 'pendiente',
    String prioridad = 'alta',
    String? sla = 'en_plazo',
    String? limite,
  }) =>
      {
        'id': 7,
        'descripcion': 'POS no enciende',
        'estado_interno': estado,
        'prioridad': prioridad,
        'estado_sla': sla,
        'limite_resolucion': limite,
        'fecha_programada': '2026-09-03T10:00:00-05:00',
        'equipos': [
          {
            'id': 3,
            'codigo': 'CR-DSK-0001',
            'modelo': 'ProDesk 400 G4',
            'numero_serie': 'MXL8192898',
          }
        ],
        'farmacia': {
          'codigo': 'ML006',
          'nombre': 'FARMACIAS MIA ML006',
          'direccion': 'Bolivar y Azuay',
          'latitud': -4.0003534,
          'longitud': -79.2018223,
        },
        'resultado_tecnico': '',
      };

  test('lee los campos snake_case de DRF', () {
    final m = Mantenimiento.desdeJson(json());
    expect(m.id, 7);
    expect(m.estado, 'pendiente');
    expect(m.prioridad, 'alta');
    expect(m.equipoPrincipal?.codigo, 'CR-DSK-0001');
    expect(m.equipoPrincipal?.numeroSerie, 'MXL8192898');
    expect(m.farmacia?.codigo, 'ML006');
    expect(m.farmacia?.tieneCoordenadas, isTrue);
  });

  test('un equipo sin farmacia no rompe', () {
    final datos = json()..['farmacia'] = null;
    final m = Mantenimiento.desdeJson(datos);
    expect(m.farmacia, isNull);
  });

  test('los estados se derivan del estado_interno', () {
    expect(Mantenimiento.desdeJson(json(estado: 'pendiente')).abierto, isTrue);
    expect(Mantenimiento.desdeJson(json(estado: 'en_proceso')).abierto, isTrue);
    expect(Mantenimiento.desdeJson(json(estado: 'cerrado')).abierto, isFalse);
    expect(Mantenimiento.desdeJson(json(estado: 'cerrado')).cerrado, isTrue);
  });

  test('un sla desconocido cae en sinSla en vez de romper', () {
    expect(Mantenimiento.desdeJson(json(sla: 'algo_nuevo')).estadoSla, EstadoSla.sinSla);
    expect(Mantenimiento.desdeJson(json(sla: null)).estadoSla, EstadoSla.sinSla);
  });

  group('tiempo restante del SLA', () {
    test('vencido lo dice explicitamente', () {
      final limite = DateTime.now().subtract(const Duration(hours: 3));
      final m = Mantenimiento.desdeJson(json(limite: limite.toIso8601String()));
      expect(m.restanteSla, contains('vencido'));
    });

    test('en plazo muestra cuanto queda', () {
      final limite = DateTime.now().add(const Duration(hours: 5));
      final m = Mantenimiento.desdeJson(json(limite: limite.toIso8601String()));
      expect(m.restanteSla, contains('quedan'));
      expect(m.restanteSla, contains('h'));
    });

    test('sin limite no inventa un texto', () {
      expect(Mantenimiento.desdeJson(json()).restanteSla, isEmpty);
    });
  });

  test('el catalogo de resultados vive en la app para poder cerrar sin conexion', () {
    // Si esto se pidiera por API, cerrar en una farmacia sin senal seria imposible.
    expect(resultadosTecnicos.containsKey('reparado'), isTrue);
    expect(resultadosTecnicos.containsKey('requiere_baja'), isTrue);
    expect(resultadosTecnicos.length, 12);
  });
}
