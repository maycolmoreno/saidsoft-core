import 'package:flutter_test/flutter_test.dart';
import 'package:mockito/mockito.dart';

import 'package:cresio_mobile/features/mantenimientos/data/mantenimientos_repository.dart';

import '../../../helpers/test_helpers.mocks.dart';

/// Fija el contrato contra el backend Django. La app venía de la API Java/Spring,
/// donde las rutas llevaban el verbo en el path (`/mantenimiento/cerrar/{id}`) y los
/// campos iban en camelCase; contra DRF eso da 404 o 400 silenciosos.
void main() {
  late MockApiClient mockApi;
  late MantenimientosRepository repository;

  setUp(() {
    mockApi = MockApiClient();
    repository = MantenimientosRepository(mockApi);
  });

  group('rutas', () {
    test('listar usa la ruta en plural con barra final', () async {
      when(mockApi.get(any)).thenAnswer((_) async => <dynamic>[]);
      await repository.listar();
      verify(mockApi.get('/mantenimientos/')).called(1);
    });

    test('obtenerDetalle usa /mantenimientos/{id}/', () async {
      when(mockApi.get(any)).thenAnswer((_) async => <String, dynamic>{});
      await repository.obtenerDetalle(7);
      verify(mockApi.get('/mantenimientos/7/')).called(1);
    });

    test('el catalogo de checklist es global, sin id', () async {
      // Al CREAR todavía no hay mantenimiento contra el que pedirlo.
      when(mockApi.get(any)).thenAnswer((_) async => <dynamic>[]);
      await repository.listarActividadesChecklist();
      verify(mockApi.get('/actividades-checklist/')).called(1);
    });

    test('el checklist de un mantenimiento va por su propia ruta', () async {
      when(mockApi.get(any)).thenAnswer((_) async => <dynamic>[]);
      await repository.listarChecklistDeMantenimiento(7);
      verify(mockApi.get('/mantenimientos/7/checklist/')).called(1);
    });

    test('iniciar marca la llegada del tecnico', () async {
      when(mockApi.post(any, any)).thenAnswer((_) async => <String, dynamic>{});
      await repository.iniciar(7);
      verify(mockApi.post('/mantenimientos/7/iniciar/', any)).called(1);
    });
  });

  group('cierre', () {
    test('manda resultado_tecnico, no texto libre', () async {
      when(mockApi.post(any, any)).thenAnswer((_) async => null);

      await repository.cerrar(
        mantenimientoId: 7,
        resultadoTecnico: 'reparado',
        tiempoRealMinutos: 45,
        estadoGeneral: 'operativo',
      );

      final capturado = verify(
        mockApi.post('/mantenimientos/7/cerrar/', captureAny),
      ).captured.single as Map<String, dynamic>;
      expect(capturado['resultado_tecnico'], 'reparado');
      expect(capturado['tiempo_real_minutos'], 45);
      expect(capturado['estado_general'], 'operativo');
    });

    test('omite los campos opcionales cuando no se cargaron', () async {
      // El backend exige tiempo_real_minutos >= 1: mandar 0 o null explícito daría
      // un 400 en vez de "no se registró".
      when(mockApi.post(any, any)).thenAnswer((_) async => null);

      await repository.cerrar(mantenimientoId: 7, resultadoTecnico: 'sin_falla');

      final capturado = verify(
        mockApi.post('/mantenimientos/7/cerrar/', captureAny),
      ).captured.single as Map<String, dynamic>;
      expect(capturado.containsKey('tiempo_real_minutos'), isFalse);
      expect(capturado.containsKey('estado_general'), isFalse);
    });
  });

  group('creacion', () {
    test('manda los nombres de campo de DRF y un solo mantenimiento', () async {
      when(mockApi.post(any, any))
          .thenAnswer((_) async => <String, dynamic>{'id': 12});

      await repository.crearVarios(
        equipoIds: [3, 4],
        custodioId: 9,
        tipoMantenimiento: '',
        fechaMantenimiento: '2026-09-01T10:00:00Z',
        detalle: 'revision',
        estadoGeneral: 'operativo',
        actividades: const [],
      );

      final capturado = verify(
        mockApi.post('/mantenimientos/', captureAny),
      ).captured.single as Map<String, dynamic>;
      expect(capturado['equipos'], [3, 4]);
      expect(capturado['cliente'], 9);
      expect(capturado['descripcion'], 'revision');
      expect(capturado['fecha_programada'], '2026-09-01T10:00:00Z');
      expect(capturado['estado_general'], 'operativo');
    });

    test('registra el checklist marcado DESPUES de crear', () async {
      // El endpoint de creación no acepta actividades: sin este paso, lo que el
      // técnico marcó en el formulario se perdía en silencio.
      when(mockApi.post(any, any))
          .thenAnswer((_) async => <String, dynamic>{'id': 12});

      await repository.crearVarios(
        equipoIds: [3],
        custodioId: 9,
        tipoMantenimiento: '',
        fechaMantenimiento: '2026-09-01T10:00:00Z',
        detalle: 'revision',
        estadoGeneral: 'operativo',
        actividades: const [
          {'id': 51, 'realizada': true},
          {'id': 52, 'realizada': false},
        ],
      );

      // Solo la marcada como realizada.
      verify(mockApi.post(
        '/mantenimientos/12/checklist/actualizar/',
        argThat(containsPair('actividad_id', 51)),
      )).called(1);
      verifyNever(mockApi.post(
        '/mantenimientos/12/checklist/actualizar/',
        argThat(containsPair('actividad_id', 52)),
      ));
    });

    test('sube las firmas con los codigos del backend', () async {
      when(mockApi.post(any, any))
          .thenAnswer((_) async => <String, dynamic>{'id': 12});

      await repository.crearVarios(
        equipoIds: [3],
        custodioId: 9,
        tipoMantenimiento: '',
        fechaMantenimiento: '2026-09-01T10:00:00Z',
        detalle: 'revision',
        estadoGeneral: 'operativo',
        actividades: const [],
        firmaTecnico: 'base64tecnico',
        firmaCustodio: 'base64custodio',
      );

      // "custodio", no "cliente": es el valor de TipoFirma en el backend.
      verify(mockApi.post(
        '/mantenimientos/12/firmar/',
        argThat(containsPair('tipo_firma', 'tecnico')),
      )).called(1);
      verify(mockApi.post(
        '/mantenimientos/12/firmar/',
        argThat(containsPair('tipo_firma', 'custodio')),
      )).called(1);
    });
  });
}
