import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:cresio_campo/comun/selector_busqueda.dart';
import 'package:cresio_campo/nucleo/catalogos.dart';

/// Con 700 farmacias, un desplegable comun construye 700 widgets para resolver cual
/// esta seleccionado: inusable, y en la practica deja la pantalla trabada.
void main() {
  /// Muchas opciones, como en produccion.
  List<Opcion> farmacias() => [
        for (var i = 1; i <= 700; i++)
          Opcion('$i', 'ML${i.toString().padLeft(3, '0')} · Farmacia $i'),
        const Opcion('9001', 'MAM06 · FARMACIAS MIA'),
      ];

  Widget envolver(Widget hijo) => MaterialApp(home: Scaffold(body: hijo));

  testWidgets('muestra el texto de vacio cuando no hay seleccion', (tester) async {
    await tester.pumpWidget(envolver(SelectorBusqueda(
      etiqueta: 'Farmacia',
      opciones: farmacias(),
      valor: null,
      textoVacio: 'Elegi la farmacia',
      onCambio: (_) {},
    )));
    expect(find.text('Elegi la farmacia'), findsOneWidget);
  });

  testWidgets('muestra la etiqueta de la opcion elegida', (tester) async {
    await tester.pumpWidget(envolver(SelectorBusqueda(
      etiqueta: 'Farmacia',
      opciones: farmacias(),
      valor: '9001',
      onCambio: (_) {},
    )));
    expect(find.text('MAM06 · FARMACIAS MIA'), findsOneWidget);
  });

  testWidgets('un valor que ya no existe no rompe: cae al texto de vacio',
      (tester) async {
    // Puede pasar si la farmacia se desactiva entre que se eligio y se reabrio.
    await tester.pumpWidget(envolver(SelectorBusqueda(
      etiqueta: 'Farmacia',
      opciones: farmacias(),
      valor: '99999',
      textoVacio: 'Todas',
      onCambio: (_) {},
    )));
    expect(find.text('Todas'), findsOneWidget);
  });

  testWidgets('al escribir acota la lista y permite elegir', (tester) async {
    String? elegido = 'nada';
    await tester.pumpWidget(envolver(SelectorBusqueda(
      etiqueta: 'Farmacia',
      opciones: farmacias(),
      valor: null,
      onCambio: (v) => elegido = v,
    )));

    await tester.tap(find.byType(InkWell));
    await tester.pumpAndSettle();

    await tester.enterText(find.byType(TextField), 'MAM06');
    await tester.pumpAndSettle();

    // Solo queda la que coincide.
    expect(find.text('MAM06 · FARMACIAS MIA'), findsOneWidget);
    expect(find.text('ML001 · Farmacia 1'), findsNothing);

    await tester.tap(find.text('MAM06 · FARMACIAS MIA'));
    await tester.pumpAndSettle();
    expect(elegido, '9001');
  });

  testWidgets('cerrar sin elegir NO borra la seleccion previa', (tester) async {
    // Devolver null al cerrar borraria la seleccion cada vez que el tecnico se
    // arrepiente; por eso vaciar es una accion explicita.
    var cambios = 0;
    await tester.pumpWidget(envolver(SelectorBusqueda(
      etiqueta: 'Farmacia',
      opciones: farmacias(),
      valor: '9001',
      onCambio: (_) => cambios++,
    )));

    await tester.tap(find.byType(InkWell));
    await tester.pumpAndSettle();
    await tester.tap(find.byIcon(Icons.close));
    await tester.pumpAndSettle();

    expect(cambios, 0);
  });

  testWidgets('vaciar es explicito y avisa con null', (tester) async {
    String? elegido = '9001';
    await tester.pumpWidget(envolver(SelectorBusqueda(
      etiqueta: 'Farmacia',
      opciones: farmacias(),
      valor: '9001',
      textoVacio: 'Todas',
      onCambio: (v) => elegido = v,
    )));

    await tester.tap(find.byType(InkWell));
    await tester.pumpAndSettle();
    // La primera fila de la lista es la de vaciar.
    await tester.tap(find.byIcon(Icons.clear));
    await tester.pumpAndSettle();

    expect(elegido, isNull);
  });

  testWidgets('sin resultados lo dice en vez de mostrar una lista vacia',
      (tester) async {
    await tester.pumpWidget(envolver(SelectorBusqueda(
      etiqueta: 'Farmacia',
      opciones: farmacias(),
      valor: null,
      onCambio: (_) {},
    )));

    await tester.tap(find.byType(InkWell));
    await tester.pumpAndSettle();
    await tester.enterText(find.byType(TextField), 'ZZZZZZ');
    await tester.pumpAndSettle();

    expect(find.textContaining('Sin resultados'), findsOneWidget);
  });
}
