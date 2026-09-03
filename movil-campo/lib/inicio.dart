import 'dart:async';

import 'package:connectivity_plus/connectivity_plus.dart';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import 'comun/tema.dart';
import 'nucleo/almacen/cola_offline.dart';
import 'nucleo/almacen/sincronizador.dart';
import 'rasgos/gps/estado_gps.dart';
import 'rasgos/gps/pantalla_gps.dart';
import 'rasgos/mantenimientos/pantalla_mantenimientos.dart';
import 'rasgos/sesion/estado_sesion.dart';
import 'rasgos/sesion/sesion.dart';
import 'rasgos/visitas/pantalla_visitas.dart';

/// Contenedor principal. Solo tres destinos: lo que el técnico hace en campo.
class Inicio extends StatefulWidget {
  const Inicio({super.key});

  @override
  State<Inicio> createState() => _InicioState();
}

class _InicioState extends State<Inicio> {
  int _indice = 0;
  int _pendientes = 0;
  StreamSubscription<List<ConnectivityResult>>? _suscripcionRed;

  @override
  void initState() {
    super.initState();
    _sincronizar();
    // Al recuperar señal se sube solo lo que quedó pendiente: el técnico no tiene
    // que acordarse de nada ni apretar un botón de "sincronizar".
    _suscripcionRed = Connectivity().onConnectivityChanged.listen((estado) {
      if (!estado.contains(ConnectivityResult.none)) _sincronizar();
    });
  }

  @override
  void dispose() {
    _suscripcionRed?.cancel();
    super.dispose();
  }

  Future<void> _sincronizar() async {
    await context.read<Sincronizador>().sincronizar();
    final pendientes = await ColaOffline.instancia.contar();
    if (mounted) setState(() => _pendientes = pendientes);
  }

  @override
  Widget build(BuildContext context) {
    final estado = context.watch<EstadoSesion>();
    final sesion = estado.sesion;
    final destinos = <Widget>[
      const PantallaMantenimientos(),
      const PantallaVisitas(),
      const PantallaGps(),
    ];

    final gpsActivo = context.watch<EstadoGps>().enviando;
    return Scaffold(
      body: Column(
        children: [
          // Que el envio este activo tiene que verse SIEMPRE, no solo en su pantalla:
          // es la diferencia entre que la presencia quede verificada o "sin datos".
          if (gpsActivo)
            Material(
              color: Tema.bien.withValues(alpha: 0.12),
              child: const SafeArea(
                bottom: false,
                child: Padding(
                  padding: EdgeInsets.symmetric(horizontal: 16, vertical: 8),
                  child: Row(
                    children: [
                      Icon(Icons.gps_fixed, size: 16, color: Tema.bien),
                      SizedBox(width: 8),
                      Text('Enviando tu ubicacion',
                          style: TextStyle(fontSize: 12, color: Tema.bien)),
                    ],
                  ),
                ),
              ),
            ),
          if (_pendientes > 0)
            Material(
              color: Tema.advertencia.withValues(alpha: 0.15),
              child: SafeArea(
                bottom: false,
                child: Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
                  child: Row(
                    children: [
                      const Icon(Icons.cloud_off, size: 18, color: Tema.advertencia),
                      const SizedBox(width: 8),
                      Expanded(
                        child: Text(
                          '$_pendientes accion(es) pendiente(s) de enviar',
                          style: const TextStyle(fontSize: 12, color: Tema.advertencia),
                        ),
                      ),
                      TextButton(
                        onPressed: _sincronizar,
                        child: const Text('Reintentar'),
                      ),
                    ],
                  ),
                ),
              ),
            ),
          Expanded(child: destinos[_indice]),
        ],
      ),
      drawer: _Menu(sesion: sesion),
      bottomNavigationBar: NavigationBar(
        selectedIndex: _indice,
        onDestinationSelected: (i) => setState(() => _indice = i),
        destinations: const [
          NavigationDestination(
            icon: Icon(Icons.build_outlined),
            selectedIcon: Icon(Icons.build),
            label: 'Trabajo',
          ),
          NavigationDestination(
            icon: Icon(Icons.storefront_outlined),
            selectedIcon: Icon(Icons.storefront),
            label: 'Visitas',
          ),
          NavigationDestination(
            icon: Icon(Icons.my_location_outlined),
            selectedIcon: Icon(Icons.my_location),
            label: 'Ubicacion',
          ),
        ],
      ),
    );
  }
}

class _Menu extends StatelessWidget {
  const _Menu({required this.sesion});
  final Sesion? sesion;

  @override
  Widget build(BuildContext context) {
    return Drawer(
      child: SafeArea(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Padding(
              padding: const EdgeInsets.all(20),
              child: Row(
                children: [
                  CircleAvatar(
                    radius: 24,
                    backgroundColor: Tema.primario,
                    child: Text(
                      sesion?.iniciales ?? '?',
                      style: const TextStyle(
                          color: Colors.white, fontWeight: FontWeight.w700),
                    ),
                  ),
                  const SizedBox(width: 12),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          sesion?.nombre ?? '',
                          style: const TextStyle(
                              fontWeight: FontWeight.w700, fontSize: 16),
                        ),
                        Text(
                          sesion?.etiquetaRol ?? '',
                          style: TextStyle(
                              fontSize: 12, color: Colors.grey.shade600),
                        ),
                      ],
                    ),
                  ),
                ],
              ),
            ),
            const Divider(height: 1),
            const Spacer(),
            ListTile(
              leading: const Icon(Icons.logout, color: Tema.critico),
              title: const Text('Cerrar sesion'),
              onTap: () {
                Navigator.of(context).pop();
                context.read<EstadoSesion>().cerrarSesion();
              },
            ),
          ],
        ),
      ),
    );
  }
}
