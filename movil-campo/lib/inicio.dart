import 'dart:async';

import 'package:connectivity_plus/connectivity_plus.dart';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import 'comun/tema.dart';
import 'nucleo/almacen/cola_offline.dart';
import 'nucleo/red/api.dart';
import 'nucleo/almacen/sincronizador.dart';
import 'rasgos/avisos/pantalla_avisos.dart';
import 'rasgos/avisos/repo_avisos.dart';
import 'rasgos/gps/estado_gps.dart';
import 'rasgos/gps/pantalla_gps.dart';
import 'rasgos/equipos/pantalla_nuevo_equipo.dart';
import 'rasgos/mantenimientos/pantalla_mantenimientos.dart';
import 'rasgos/mantenimientos/pantalla_nuevo.dart';
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
  int _avisos = 0;
  StreamSubscription<List<ConnectivityResult>>? _suscripcionRed;

  @override
  void initState() {
    super.initState();
    _sincronizar();
    _contarAvisos();
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

  /// Contador de la bandeja. Falla en silencio: un aviso no visto es molesto, pero
  /// una pantalla rota por no poder contarlos es peor.
  Future<void> _contarAvisos() async {
    try {
      final total = await RepoAvisos(context.read<Api>()).sinLeer();
      if (mounted) setState(() => _avisos = total);
    } catch (_) {
      // Sin conexión o sin permiso: el contador queda como estaba.
    }
  }

  /// Abre un alta y refresca al volver, para que lo recien creado se vea sin que el
  /// tecnico tenga que deslizar.
  Future<void> _abrirAlta(Widget pantalla) async {
    await Navigator.of(context).push(MaterialPageRoute(builder: (_) => pantalla));
    if (mounted) setState(() {});
    await _contarAvisos();
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
      const PantallaAvisos(),
      const PantallaGps(),
    ];

    final gpsActivo = context.watch<EstadoGps>().enviando;
    const titulos = ['Mis mantenimientos', 'Mis visitas', 'Avisos', 'Mi ubicacion'];
    return Scaffold(
      // La barra la arma ACA y no cada pantalla: si cada una trae su propio
      // Scaffold, el cajon del menu queda tapado y sin boton para abrirlo --
      // dejaba la app sin forma de cerrar sesion.
      appBar: AppBar(title: Text(titulos[_indice])),
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
      // Solo en la pestana de trabajo: el boton crea lo que esa pestana lista.
      floatingActionButton: _indice == 0 && estado.puede(Permiso.crearMantenimiento)
          ? FloatingActionButton.extended(
              onPressed: () => _abrirAlta(const PantallaNuevoMantenimiento()),
              icon: const Icon(Icons.add),
              label: const Text('Mantenimiento'),
            )
          : null,
      drawer: _Menu(
        sesion: sesion,
        onRegistrarEquipo: () => _abrirAlta(const PantallaNuevoEquipo()),
      ),
      bottomNavigationBar: NavigationBar(
        selectedIndex: _indice,
        onDestinationSelected: (i) {
          setState(() => _indice = i);
          // Al entrar a la bandeja se refresca el contador: si el técnico marca
          // avisos como leídos, el badge tiene que bajar.
          if (i == 2) _contarAvisos();
        },
        destinations: [
          const NavigationDestination(
            icon: Icon(Icons.build_outlined),
            selectedIcon: Icon(Icons.build),
            label: 'Trabajo',
          ),
          const NavigationDestination(
            icon: Icon(Icons.storefront_outlined),
            selectedIcon: Icon(Icons.storefront),
            label: 'Visitas',
          ),
          NavigationDestination(
            icon: Badge(
              isLabelVisible: _avisos > 0,
              label: Text('$_avisos'),
              child: const Icon(Icons.notifications_none),
            ),
            selectedIcon: const Icon(Icons.notifications),
            label: 'Avisos',
          ),
          const NavigationDestination(
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
  const _Menu({required this.sesion, required this.onRegistrarEquipo});
  final Sesion? sesion;
  final VoidCallback onRegistrarEquipo;

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
            ListTile(
              leading: const Icon(Icons.add_box_outlined, color: Tema.primario),
              title: const Text('Registrar equipo'),
              subtitle: const Text('Inventariar uno que no esta cargado'),
              onTap: () {
                Navigator.of(context).pop();
                onRegistrarEquipo();
              },
            ),
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
