import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import 'comun/tema.dart';
import 'nucleo/almacen/almacen_seguro.dart';
import 'nucleo/almacen/sincronizador.dart';
import 'nucleo/red/api.dart';
import 'rasgos/gps/estado_gps.dart';
import 'rasgos/gps/repo_gps.dart';
import 'rasgos/sesion/estado_sesion.dart';
import 'rasgos/sesion/pantalla_ingreso.dart';
import 'rasgos/sesion/pantalla_servidor.dart';
import 'rasgos/sesion/repo_sesion.dart';
import 'inicio.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const AppCampo());
}

class AppCampo extends StatefulWidget {
  const AppCampo({super.key});

  @override
  State<AppCampo> createState() => _AppCampoState();
}

class _AppCampoState extends State<AppCampo> {
  late final AlmacenSeguro _almacen;
  late final Api _api;
  late final EstadoSesion _sesion;

  @override
  void initState() {
    super.initState();
    _almacen = AlmacenSeguro();
    // El cliente avisa a EstadoSesion cuando el servidor devuelve 401, para que la
    // app vuelva al ingreso en vez de dejar al técnico tocando botones muertos.
    _api = Api(
      almacen: _almacen,
      alExpirarSesion: () async => _sesion.sesionExpirada(),
    );
    _sesion = EstadoSesion(RepoSesion(almacen: _almacen, api: _api));
    _sesion.arrancar();
  }

  @override
  Widget build(BuildContext context) {
    return MultiProvider(
      providers: [
        Provider<AlmacenSeguro>.value(value: _almacen),
        Provider<Api>.value(value: _api),
        Provider<Sincronizador>(create: (_) => Sincronizador(_api)),
        // A nivel de app: si colgara de la pantalla de GPS, el envio se cortaria
        // apenas el tecnico cambia de pestana para trabajar.
        ChangeNotifierProvider<EstadoGps>(create: (_) => EstadoGps(RepoGps(_api))),
        ChangeNotifierProvider<EstadoSesion>.value(value: _sesion),
      ],
      child: MaterialApp(
        title: 'SAIDSOFT Campo',
        debugShowCheckedModeBanner: false,
        theme: Tema.claro(),
        home: const _Puerta(),
      ),
    );
  }
}

/// Decide qué mostrar según el estado de la sesión.
class _Puerta extends StatelessWidget {
  const _Puerta();

  @override
  Widget build(BuildContext context) {
    final fase = context.watch<EstadoSesion>().fase;
    return switch (fase) {
      FaseSesion.cargando => const Scaffold(
          body: Center(child: CircularProgressIndicator()),
        ),
      FaseSesion.sinServidor => const PantallaServidor(),
      FaseSesion.sinSesion => const PantallaIngreso(),
      FaseSesion.autenticado => const Inicio(),
    };
  }
}
