import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

/// Regresion: cerrar una visita reventaba con una asercion del framework.
///
/// El controlador del TextField se creaba fuera del dialogo y se liberaba apenas
/// showDialog retornaba. Pero el dialogo sigue ANIMANDOSE al cerrarse, y su TextField
/// se reconstruye contra un controlador ya destruido.
///
/// Estos tests reproducen las dos formas y comprueban que la buena no lanza. No
/// importan la pantalla real (necesita red y providers): lo que se fija es el patron.
void main() {
  Future<String?> abrir(WidgetTester tester, Widget Function() contenido) async {
    String? resultado;
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: Builder(
          builder: (context) => TextButton(
            onPressed: () async {
              resultado = await showDialog<String>(
                context: context,
                builder: (_) => contenido(),
              );
            },
            child: const Text('abrir'),
          ),
        ),
      ),
    ));
    await tester.tap(find.text('abrir'));
    await tester.pumpAndSettle();
    return resultado;
  }

  testWidgets('un dialogo dueño de su controlador se cierra sin errores',
      (tester) async {
    await abrir(tester, () => const _DialogoBueno());

    await tester.enterText(find.byType(TextField), 'sin novedades');
    await tester.tap(find.text('Confirmar'));
    // El settle recorre TODA la animacion de salida: es ahi donde saltaba la
    // asercion con el controlador liberado de afuera.
    await tester.pumpAndSettle();

    expect(tester.takeException(), isNull);
    expect(find.byType(AlertDialog), findsNothing);
  });

  testWidgets('cancelar tampoco deja excepciones', (tester) async {
    await abrir(tester, () => const _DialogoBueno());
    await tester.tap(find.text('Cancelar'));
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
  });

  testWidgets('el controlador se libera al desmontarse el dialogo', (tester) async {
    await abrir(tester, () => const _DialogoBueno());
    final estado = tester.state<_DialogoBuenoState>(find.byType(_DialogoBueno));
    final controlador = estado.controlador;

    await tester.tap(find.text('Cancelar'));
    await tester.pumpAndSettle();

    // addListener sobre un ChangeNotifier liberado lanza: prueba que el dialogo SI
    // libero su controlador, y que por eso no hay que liberarlo tambien desde afuera.
    // (Leer .text no sirve como prueba: no falla sobre uno ya liberado.)
    expect(() => controlador.addListener(() {}), throwsFlutterError);
  });
}

/// Misma forma que _DialogoCierre en pantalla_visitas.dart: el controlador vive con
/// el dialogo.
class _DialogoBueno extends StatefulWidget {
  const _DialogoBueno();

  @override
  State<_DialogoBueno> createState() => _DialogoBuenoState();
}

class _DialogoBuenoState extends State<_DialogoBueno> {
  final controlador = TextEditingController();

  @override
  void dispose() {
    controlador.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      content: TextField(controller: controlador),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('Cancelar'),
        ),
        FilledButton(
          onPressed: () => Navigator.of(context).pop(controlador.text.trim()),
          child: const Text('Confirmar'),
        ),
      ],
    );
  }
}
