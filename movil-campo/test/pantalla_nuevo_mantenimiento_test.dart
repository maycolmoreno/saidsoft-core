import 'dart:convert';
import 'dart:io';

import 'package:cresio_campo/nucleo/almacen/almacen_seguro.dart';
import 'package:cresio_campo/nucleo/catalogos.dart';
import 'package:cresio_campo/nucleo/red/api.dart';
import 'package:cresio_campo/rasgos/mantenimientos/pantalla_nuevo.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';

/// Reproduce "Nuevo mantenimiento sale en blanco" reportado desde el celular
/// (4-sep-2026): la pantalla mostraba solo la barra de título y un cuerpo vacío, sin
/// formulario, sin error y sin spinner.
void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  Future<void> montar(WidgetTester tester, RepoCatalogos repo) async {
    await tester.pumpWidget(
      MultiProvider(
        providers: [
          Provider<Api>(create: (_) => Api(almacen: AlmacenSeguro())),
          Provider<RepoCatalogos>.value(value: repo),
        ],
        child: const MaterialApp(home: PantallaNuevoMantenimiento()),
      ),
    );
  }

  testWidgets('con catalogos cargados muestra el formulario, no un cuerpo vacio',
      (tester) async {
    await montar(tester, _RepoFalso(_catalogosDePrueba()));
    await tester.pumpAndSettle();

    expect(find.text('1. Busca el equipo'), findsOneWidget);
    expect(find.byType(TextField), findsWidgets);
    expect(find.text('Buscar'), findsOneWidget);
  });

  testWidgets('mientras carga muestra el indicador, no una pantalla en blanco',
      (tester) async {
    await montar(tester, _RepoFalso(_catalogosDePrueba(), demora: const Duration(seconds: 1)));
    await tester.pump();

    expect(find.byType(CircularProgressIndicator), findsOneWidget);
    await tester.pumpAndSettle();
  });

  testWidgets('si el catalogo falla lo dice y ofrece reintentar', (tester) async {
    await montar(tester, _RepoFalso(null, error: const SinConexion()));
    await tester.pumpAndSettle();

    expect(find.text('Sin conexion'), findsOneWidget);
    expect(find.text('Reintentar'), findsOneWidget);
  });

  testWidgets('con el catalogo REAL de produccion tampoco queda en blanco',
      (tester) async {
    // 700 farmacias, tildes, nombres vacios: los datos de verdad, no unos inventados
    // que casualmente no tienen el caso raro.
    final crudo = File('test/datos/catalogos_produccion.json').readAsStringSync();
    final catalogos = Catalogos.desdeJson(
      Map<String, dynamic>.from(jsonDecode(crudo) as Map),
    );
    await montar(tester, _RepoFalso(catalogos));
    await tester.pumpAndSettle();

    expect(find.text('1. Busca el equipo'), findsOneWidget);
    expect(find.text('Buscar'), findsOneWidget);
  });

  testWidgets('700 farmacias no dejan la pantalla vacia ni la cuelgan', (tester) async {
    // El caso real: el selector tiene que aguantar el catalogo completo.
    await montar(tester, _RepoFalso(_catalogosDePrueba(farmacias: 700)));
    await tester.pumpAndSettle();

    expect(find.text('1. Busca el equipo'), findsOneWidget);
  });
}

Catalogos _catalogosDePrueba({int farmacias = 3}) => Catalogos(
      tiposEquipo: const [Opcion('DSK', 'Desktop')],
      marcas: const [Opcion('1', 'HP')],
      categorias: const [Opcion('1', 'POS')],
      tiposMantenimiento: const [Opcion('1', 'Preventivo')],
      estadosGenerales: const [Opcion('operativo', 'Operativo')],
      prioridades: const [Opcion('normal', 'Normal')],
      farmacias: List.generate(farmacias, (i) => Opcion('$i', 'ML${i.toString().padLeft(3, '0')}')),
      bodegas: const [Opcion('1', 'BOD-LOJ')],
      colaboradores: const [Opcion('1', 'Tecnico Uno')],
      tiposConsumible: const [Opcion('1', 'Toner negro')],
    );

class _RepoFalso implements RepoCatalogos {
  _RepoFalso(this._datos, {this.error, this.demora});

  final Catalogos? _datos;
  final Object? error;
  final Duration? demora;

  @override
  Future<Catalogos> obtener() async {
    if (demora != null) await Future<void>.delayed(demora!);
    if (error != null) throw error!;
    return _datos!;
  }
}
