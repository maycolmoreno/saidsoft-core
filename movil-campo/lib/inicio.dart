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
    // La barra se arma SOLO con lo que el técnico puede usar: mostrar una pestaña
    // que al tocarla responde 403 es peor que no mostrarla, porque parece una falla
    // del sistema y no una restricción de permisos.
    //
    // Avisos no se filtra: la bandeja es del propio usuario, no de un modelo que
    // haya que tener permiso para ver.
    final destinos = <_Destino>[
      if (estado.puede(Permiso.verMantenimientos))
        const _Destino(
          titulo: 'Mis mantenimientos',
          etiqueta: 'Trabajo',
          icono: Icons.build_outlined,
          iconoActivo: Icons.build,
          pantalla: PantallaMantenimientos(),
        ),
      if (estado.puede(Permiso.verVisitas))
        const _Destino(
          titulo: 'Mis visitas',
          etiqueta: 'Visitas',
          icono: Icons.storefront_outlined,
          iconoActivo: Icons.storefront,
          pantalla: PantallaVisitas(),
        ),
      const _Destino(
        titulo: 'Avisos',
        etiqueta: 'Avisos',
        icono: Icons.notifications_none,
        iconoActivo: Icons.notifications,
        pantalla: PantallaAvisos(),
        conBadge: true,
      ),
      if (estado.puede(Permiso.enviarUbicacion))
        const _Destino(
          titulo: 'Mi ubicacion',
          etiqueta: 'Ubicacion',
          icono: Icons.my_location_outlined,
          iconoActivo: Icons.my_location,
          pantalla: PantallaGps(),
        ),
    ];

    // El indice guardado puede quedar fuera de rango si cambian los permisos entre
    // sesiones; sin esto la app reventaria al abrir.
    final indice = _indice < destinos.length ? _indice : 0;
    final gpsActivo = context.watch<EstadoGps>().enviando;
    return Scaffold(
      // La barra la arma ACA y no cada pantalla: si cada una trae su propio
      // Scaffold, el cajon del menu queda tapado y sin boton para abrirlo --
      // dejaba la app sin forma de cerrar sesion.
      appBar: AppBar(title: Text(destinos[indice].titulo)),
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
          Expanded(child: destinos[indice].pantalla),
        ],
      ),
      // Solo sobre la lista de mantenimientos, y solo si puede crearlos: el boton
      // crea lo que esa pantalla lista.
      floatingActionButton: destinos[indice].pantalla is PantallaMantenimientos &&
              estado.puede(Permiso.crearMantenimiento)
          ? FloatingActionButton.extended(
              onPressed: () => _abrirAlta(const PantallaNuevoMantenimiento()),
              icon: const Icon(Icons.add),
              label: const Text('Mantenimiento'),
            )
          : null,
      drawer: _Menu(
        sesion: sesion,
        // Sin permiso para dar de alta activos, la opcion no aparece.
        onRegistrarEquipo: estado.puede(Permiso.registrarEquipo)
            ? () => _abrirAlta(const PantallaNuevoEquipo())
            : null,
      ),
      // Una sola pestana no es una barra: ocupa lugar sin ofrecer nada que elegir.
      bottomNavigationBar: destinos.length < 2
          ? null
          : NavigationBar(
              selectedIndex: indice,
              onDestinationSelected: (i) {
                setState(() => _indice = i);
                // Al entrar a la bandeja se refresca el contador: si el tecnico
                // marca avisos como leidos, el badge tiene que bajar.
                if (destinos[i].conBadge) _contarAvisos();
              },
              destinations: [
                for (final d in destinos)
                  NavigationDestination(
                    icon: d.conBadge
                        ? Badge(
                            isLabelVisible: _avisos > 0,
                            label: Text('$_avisos'),
                            child: Icon(d.icono),
                          )
                        : Icon(d.icono),
                    selectedIcon: Icon(d.iconoActivo),
                    label: d.etiqueta,
                  ),
              ],
            ),
    );
  }
}

/// Una pestaña de la barra inferior. Se declara junto con el permiso que la habilita
/// (ver build) para que agregar una sección obligue a decidir quién la ve.
class _Destino {
  const _Destino({
    required this.titulo,
    required this.etiqueta,
    required this.icono,
    required this.iconoActivo,
    required this.pantalla,
    this.conBadge = false,
  });

  /// Encabezado de la barra superior.
  final String titulo;

  /// Texto corto bajo el icono.
  final String etiqueta;
  final IconData icono;
  final IconData iconoActivo;
  final Widget pantalla;

  /// Si lleva el contador de avisos sin leer.
  final bool conBadge;
}

class _Menu extends StatefulWidget {
  const _Menu({required this.sesion, required this.onRegistrarEquipo});
  final Sesion? sesion;

  /// null cuando el usuario no puede dar de alta activos: la opcion no se dibuja.
  final VoidCallback? onRegistrarEquipo;

  @override
  State<_Menu> createState() => _MenuState();
}

class _MenuState extends State<_Menu> {
  /// null mientras se le pregunta al sistema. Se ofrece el interruptor SOLO si el
  /// telefono ya tiene una huella registrada: mostrarlo en un equipo sin biometria
  /// seria ofrecer un boton que no puede hacer nada.
  bool? _biometriaUsable;

  @override
  void initState() {
    super.initState();
    _consultar();
  }

  Future<void> _consultar() async {
    final estado = context.read<EstadoSesion>();
    await estado.refrescarBloqueo();
    final usable = await estado.biometriaUsable();
    if (mounted) setState(() => _biometriaUsable = usable);
  }

  Future<void> _cambiar(bool valor) async {
    final estado = context.read<EstadoSesion>();
    final ok = await estado.cambiarBloqueo(valor);
    if (!mounted) return;
    if (!ok) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('No se pudo verificar tu huella. El bloqueo sigue igual.')),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    final sesion = widget.sesion;
    final onRegistrarEquipo = widget.onRegistrarEquipo;
    final bloqueoActivo = context.watch<EstadoSesion>().bloqueoActivo;
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
            if (onRegistrarEquipo != null)
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
            if (_biometriaUsable == true)
              SwitchListTile(
                secondary: const Icon(Icons.fingerprint, color: Tema.primario),
                title: const Text('Entrar con huella'),
                subtitle: const Text('Pide tu huella al abrir la app'),
                value: bloqueoActivo,
                onChanged: _cambiar,
              ),
            const Divider(height: 1),
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
