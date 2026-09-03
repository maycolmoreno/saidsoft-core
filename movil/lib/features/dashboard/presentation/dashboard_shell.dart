import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../../core/network/api_client.dart';
import '../../../shared/theme/app_theme.dart';
import '../../auth/data/auth_models.dart';
import '../../auth/presentation/auth_provider.dart';
import '../../configuracion/presentation/settings_screen.dart';
import '../../equipos/presentation/equipos_screen.dart';
import '../../gps/presentation/gps_provider.dart';
import '../../gps/presentation/consentimiento_gps_screen.dart';
import '../../gps/presentation/ubicaciones_realtime_screen.dart';
import '../../mantenimientos/data/mantenimientos_repository.dart';
import '../../mantenimientos/presentation/mantenimientos_screen.dart';
import '../../notificaciones/data/notificaciones_repository.dart';
import '../../notificaciones/data/push_notificacion_service.dart';
import '../../notificaciones/presentation/notificaciones_screen.dart';
import '../../planificacion/presentation/planificacion_screen.dart';
import '../../ubicaciones/presentation/ubicaciones_screen.dart';
import '../../visitas/presentation/visitas_screen.dart';
import 'dashboard_provider.dart';

class DashboardShell extends StatefulWidget {
  const DashboardShell({super.key});

  @override
  State<DashboardShell> createState() => _DashboardShellState();
}

class _DashboardShellState extends State<DashboardShell>
    with WidgetsBindingObserver {
  int _index = 0;
  bool _promptMonitoreoMostrado = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _enviarUbicacionInicial();
    _inicializarPush();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      context.read<AuthProvider>().refreshSession();
      _mostrarAlertaMonitoreoSiAplica();
    });
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed && mounted) {
      context.read<AuthProvider>().refreshSession();
    }
  }

  Future<void> _inicializarPush() async {
    try {
      await context.read<PushNotificacionService>().inicializar();
    } catch (e) {
      debugPrint('Error inicializando push: $e');
    }
  }

  Future<void> _enviarUbicacionInicial() async {
    final auth = context.read<AuthProvider>();
    if (!auth.hasCapability(UserCapability.sendGpsLocation)) return;
    final userId = auth.userId;
    if (userId == null) return;
    final gps = context.read<GpsProvider>();
    await gps.enviarUbicacionAlIngreso(userId);
  }

  Future<void> _mostrarAlertaMonitoreoSiAplica() async {
    if (_promptMonitoreoMostrado || !mounted) {
      return;
    }

    final auth = context.read<AuthProvider>();
    if (!auth.hasCapability(UserCapability.sendGpsLocation)) {
      return;
    }
    final userId = auth.userId;
    if (userId == null) {
      return;
    }

    final gps = context.read<GpsProvider>();
    await gps.cargarConsentimientoRegistrado(userId);
    if (!mounted || gps.consentimientoRegistrado) {
      return;
    }

    _promptMonitoreoMostrado = true;

    final abrirMonitoreo = await showDialog<bool>(
          context: context,
          builder: (dialogContext) => AlertDialog(
            title: const Text('Monitoreo en tiempo real'),
            content: const Text(
              'Para continuar con tus actividades en campo, '
              'necesitamos tu consentimiento para monitorear tu ubicacion en tiempo real.',
            ),
            actions: [
              TextButton(
                onPressed: () => Navigator.of(dialogContext).pop(false),
                child: const Text('Mas tarde'),
              ),
              FilledButton(
                onPressed: () => Navigator.of(dialogContext).pop(true),
                child: const Text('Continuar'),
              ),
            ],
          ),
        ) ??
        false;

    if (!mounted || !abrirMonitoreo) {
      return;
    }

    await Navigator.of(context).push(
      MaterialPageRoute(builder: (_) => const ConsentimientoGpsScreen()),
    );
  }

  @override
  Widget build(BuildContext context) {
    return ChangeNotifierProvider(
      create: (context) {
        final apiClient = context.read<ApiClient>();
        return DashboardProvider(
          apiClient,
          mantenimientosRepository: MantenimientosRepository(apiClient),
          notificacionesRepository: NotificacionesRepository(apiClient),
        );
      },
      child: Consumer<DashboardProvider>(
        builder: (context, dashboard, _) {
          final auth = context.watch<AuthProvider>();
          final tabs = <_ShellTab>[
            _ShellTab(
              title: 'CRESIO',
              icon: Icons.home_rounded,
              label: 'Inicio',
              page: _HomeTab(dashboard: dashboard, auth: auth),
            ),
          ];
          if (auth.hasCapability(UserCapability.viewMantenimientos)) {
            tabs.add(const _ShellTab(
              title: 'Mantenimientos',
              icon: Icons.build_circle_outlined,
              label: 'Mantenim.',
              page: MantenimientosScreen(),
            ));
          }
          if (auth.hasCapability(UserCapability.viewVisitas)) {
            tabs.add(const _ShellTab(
              title: 'Visita tecnica',
              icon: Icons.assignment_outlined,
              label: 'Visitas',
              page: VisitasScreen(),
            ));
          }
          if (auth.hasCapability(UserCapability.viewEquipos)) {
            tabs.add(const _ShellTab(
              title: 'Equipos',
              icon: Icons.devices_outlined,
              label: 'Equipos',
              page: EquiposScreen(),
            ));
          }
          if (auth.hasCapability(UserCapability.viewPlanificacion)) {
            tabs.add(const _ShellTab(
              title: 'Planificacion',
              icon: Icons.event_note_outlined,
              label: 'Planificar',
              page: PlanificacionScreen(),
            ));
          }
          tabs.add(const _ShellTab(
            title: 'Mas',
            icon: Icons.grid_view_rounded,
            label: 'Mas',
            page: _MoreTab(),
          ));

          final currentIndex = _index >= tabs.length ? tabs.length - 1 : _index;

          return Scaffold(
            body: currentIndex == 0
                ? RefreshIndicator(
                    onRefresh: dashboard.refresh,
                    child: tabs[currentIndex].page,
                  )
                : tabs[currentIndex].page,
            bottomNavigationBar: Container(
              decoration: BoxDecoration(
                color: Colors.white,
                boxShadow: [
                  BoxShadow(
                    color: Colors.black.withValues(alpha: 0.06),
                    blurRadius: 10,
                    offset: const Offset(0, -2),
                  ),
                ],
              ),
              child: NavigationBar(
                selectedIndex: currentIndex,
                onDestinationSelected: (v) => setState(() => _index = v),
                destinations: tabs
                    .map((t) => NavigationDestination(
                          icon: Icon(t.icon),
                          label: t.label,
                        ))
                    .toList(),
              ),
            ),
            floatingActionButton: currentIndex == 0 &&
                    auth.hasCapability(UserCapability.createMantenimiento)
                ? FloatingActionButton(
                    onPressed: () => setState(() => _index =
                        tabs.indexWhere((t) => t.title == 'Mantenimientos')),
                    child: const Icon(Icons.add),
                  )
                : null,
          );
        },
      ),
    );
  }
}

class _ShellTab {
  const _ShellTab({
    required this.title,
    required this.icon,
    required this.label,
    required this.page,
  });
  final String title;
  final IconData icon;
  final String label;
  final Widget page;
}

// ─── HOME TAB ──────────────────────────────────────────────────────────────

class _HomeTab extends StatelessWidget {
  const _HomeTab({required this.dashboard, required this.auth});
  final DashboardProvider dashboard;
  final AuthProvider auth;

  @override
  Widget build(BuildContext context) {
    return CustomScrollView(
      physics: const AlwaysScrollableScrollPhysics(),
      slivers: [
        SliverToBoxAdapter(child: _Header(auth: auth, dashboard: dashboard)),
        if (dashboard.offline)
          const SliverToBoxAdapter(
            child: _AlertBanner(
              icon: Icons.cloud_off_rounded,
              text: 'Sin conexion — modo offline',
              color: Color(0xFFF59E0B),
              bgColor: Color(0xFFFFFBEB),
            ),
          ),
        if (dashboard.error != null)
          SliverToBoxAdapter(
            child: _AlertBanner(
              icon: Icons.warning_amber_rounded,
              text: dashboard.error!,
              color: AppTheme.danger,
              bgColor: const Color(0xFFFEF2F2),
            ),
          ),
        // Metric cards
        SliverPadding(
          padding: const EdgeInsets.fromLTRB(16, 8, 16, 8),
          sliver: SliverGrid.count(
            crossAxisCount: 2,
            mainAxisSpacing: 12,
            crossAxisSpacing: 12,
            childAspectRatio: 1.35,
            children: [
              _MetricCard(
                icon: Icons.build_circle,
                label: 'Mant. abiertos',
                value: '${dashboard.openMantenimientos}',
                gradient: const [Color(0xFF47267F), Color(0xFF5C36A0)],
              ),
              _MetricCard(
                icon: Icons.devices,
                label: 'Equipos activos',
                value: '${dashboard.activeEquipos}',
                gradient: const [Color(0xFFE8412D), Color(0xFFF05C2E)],
              ),
              _MetricCard(
                icon: Icons.notifications_active,
                label: 'Notificaciones',
                value: '${dashboard.pendingNotifications}',
                gradient: const [Color(0xFFF5A623), Color(0xFFF39C12)],
              ),
              _MetricCard(
                icon: Icons.cloud_sync,
                label: 'Pend. offline',
                value: '${dashboard.pendingOffline}',
                gradient: const [Color(0xFF6C5CE7), Color(0xFFA29BFE)],
              ),
            ],
          ),
        ),
        // Quick actions
        SliverToBoxAdapter(
          child: Padding(
            padding: const EdgeInsets.fromLTRB(16, 12, 16, 8),
            child: Text('Acciones rapidas',
                style: Theme.of(context).textTheme.titleMedium),
          ),
        ),
        SliverToBoxAdapter(
          child: SizedBox(
            height: 90,
            child: ListView(
              scrollDirection: Axis.horizontal,
              padding: const EdgeInsets.symmetric(horizontal: 12),
              children: [
                if (auth.hasCapability(UserCapability.viewNotificaciones))
                  _QuickAction(
                    icon: Icons.notifications_outlined,
                    label: 'Notificaciones',
                    badge: dashboard.pendingNotifications,
                    onTap: () => Navigator.of(context).push(MaterialPageRoute(
                        builder: (_) => const NotificacionesScreen())),
                  ),
                if (auth.hasCapability(UserCapability.viewVisitas))
                  _QuickAction(
                    icon: Icons.assignment_turned_in_outlined,
                    label: 'Visitas',
                    onTap: () => Navigator.of(context).push(MaterialPageRoute(
                        builder: (_) => const VisitasScreen())),
                  ),
                if (auth.hasCapability(UserCapability.sendGpsLocation))
                  _QuickAction(
                    icon: Icons.gps_fixed,
                    label: 'GPS',
                    onTap: () => Navigator.of(context).push(MaterialPageRoute(
                        builder: (_) => const ConsentimientoGpsScreen())),
                  ),
                if (auth.hasCapability(UserCapability.viewGpsRealtime))
                  _QuickAction(
                    icon: Icons.map_outlined,
                    label: 'Tiempo real',
                    onTap: () => Navigator.of(context).push(MaterialPageRoute(
                        builder: (_) => const UbicacionesRealtimeScreen())),
                  ),
                _QuickAction(
                  icon: Icons.settings_outlined,
                  label: 'Ajustes',
                  onTap: () => Navigator.of(context).push(MaterialPageRoute(
                      builder: (_) => const SettingsScreen())),
                ),
              ],
            ),
          ),
        ),
        // Recent activity
        SliverToBoxAdapter(
          child: Padding(
            padding: const EdgeInsets.fromLTRB(16, 16, 16, 8),
            child: Text('Actividad reciente',
                style: Theme.of(context).textTheme.titleMedium),
          ),
        ),
        if (dashboard.recentMantenimientos.isEmpty)
          const SliverToBoxAdapter(
            child: Padding(
              padding: EdgeInsets.symmetric(horizontal: 16),
              child: Card(
                child: Padding(
                  padding: EdgeInsets.all(24),
                  child: Center(
                    child: Column(
                      children: [
                        Icon(Icons.inbox_outlined,
                            size: 40, color: Color(0xFFBBBBBB)),
                        SizedBox(height: 8),
                        Text('No hay actividad reciente',
                            style: TextStyle(color: Color(0xFF888888))),
                      ],
                    ),
                  ),
                ),
              ),
            ),
          )
        else
          SliverPadding(
            padding: const EdgeInsets.symmetric(horizontal: 16),
            sliver: SliverList(
              delegate: SliverChildBuilderDelegate(
                (context, index) {
                  final item = dashboard.recentMantenimientos[index];
                  return _RecentActivityCard(item: item);
                },
                childCount: dashboard.recentMantenimientos.length,
              ),
            ),
          ),
        const SliverPadding(padding: EdgeInsets.only(bottom: 80)),
      ],
    );
  }
}

// ─── HEADER ────────────────────────────────────────────────────────────────

class _Header extends StatelessWidget {
  const _Header({required this.auth, required this.dashboard});
  final AuthProvider auth;
  final DashboardProvider dashboard;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: EdgeInsets.only(
        top: MediaQuery.of(context).padding.top + 16,
        left: 20,
        right: 20,
        bottom: 20,
      ),
      decoration: const BoxDecoration(
        gradient: LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: [Color(0xFF2F1857), Color(0xFF47267F)],
        ),
        borderRadius: BorderRadius.vertical(bottom: Radius.circular(24)),
      ),
      child: Row(
        children: [
          CircleAvatar(
            radius: 24,
            backgroundColor: Colors.white.withValues(alpha: 0.2),
            child: Text(
              (auth.session?.displayName ?? 'U')[0].toUpperCase(),
              style: const TextStyle(
                  color: Colors.white,
                  fontSize: 20,
                  fontWeight: FontWeight.w700),
            ),
          ),
          const SizedBox(width: 14),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  'Hola, ${auth.session?.displayName ?? 'Usuario'}',
                  style: const TextStyle(
                    color: Colors.white,
                    fontSize: 18,
                    fontWeight: FontWeight.w600,
                  ),
                ),
                const SizedBox(height: 2),
                Container(
                  padding:
                      const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
                  decoration: BoxDecoration(
                    color: Colors.white.withValues(alpha: 0.15),
                    borderRadius: BorderRadius.circular(8),
                  ),
                  child: Text(
                    auth.roleLabel,
                    style: TextStyle(
                      color: Colors.white.withValues(alpha: 0.9),
                      fontSize: 12,
                    ),
                  ),
                ),
              ],
            ),
          ),
          IconButton(
            onPressed: () => Navigator.of(context).push(
              MaterialPageRoute(builder: (_) => const NotificacionesScreen()),
            ),
            icon: Badge(
              isLabelVisible: dashboard.pendingNotifications > 0,
              label: Text('${dashboard.pendingNotifications}'),
              child: const Icon(Icons.notifications_outlined,
                  color: Colors.white, size: 26),
            ),
          ),
        ],
      ),
    );
  }
}

// ─── METRIC CARD ───────────────────────────────────────────────────────────

class _MetricCard extends StatelessWidget {
  const _MetricCard({
    required this.icon,
    required this.label,
    required this.value,
    required this.gradient,
  });
  final IconData icon;
  final String label;
  final String value;
  final List<Color> gradient;

  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: BoxDecoration(
        gradient: LinearGradient(colors: gradient),
        borderRadius: BorderRadius.circular(16),
        boxShadow: [
          BoxShadow(
            color: gradient.first.withValues(alpha: 0.3),
            blurRadius: 8,
            offset: const Offset(0, 4),
          ),
        ],
      ),
      padding: const EdgeInsets.all(14),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisAlignment: MainAxisAlignment.spaceBetween,
        mainAxisSize: MainAxisSize.max,
        children: [
          Icon(icon, color: Colors.white.withValues(alpha: 0.85), size: 22),
          Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(value,
                  style: const TextStyle(
                      color: Colors.white,
                      fontSize: 26,
                      fontWeight: FontWeight.w800)),
              const SizedBox(height: 2),
              Text(
                label,
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(
                  color: Colors.white.withValues(alpha: 0.85),
                  fontSize: 12,
                  height: 1.15,
                ),
              ),
            ],
          ),
        ],
      ),
    );
  }
}

// ─── QUICK ACTION ──────────────────────────────────────────────────────────

class _QuickAction extends StatelessWidget {
  const _QuickAction({
    required this.icon,
    required this.label,
    required this.onTap,
    this.badge = 0,
  });
  final IconData icon;
  final String label;
  final VoidCallback onTap;
  final int badge;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 4),
      child: InkWell(
        borderRadius: BorderRadius.circular(14),
        onTap: onTap,
        child: Container(
          width: 78,
          padding: const EdgeInsets.symmetric(vertical: 10),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Badge(
                isLabelVisible: badge > 0,
                label: Text('$badge'),
                child: Container(
                  padding: const EdgeInsets.all(10),
                  decoration: BoxDecoration(
                    color: const Color(0xFFF0F4F8),
                    borderRadius: BorderRadius.circular(12),
                  ),
                  child: Icon(icon, size: 22, color: const Color(0xFF47267F)),
                ),
              ),
              const SizedBox(height: 6),
              Text(label,
                  textAlign: TextAlign.center,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(fontSize: 11)),
            ],
          ),
        ),
      ),
    );
  }
}

// ─── RECENT ACTIVITY CARD ──────────────────────────────────────────────────

class _RecentActivityCard extends StatelessWidget {
  const _RecentActivityCard({required this.item});
  final Map<String, dynamic> item;

  @override
  Widget build(BuildContext context) {
    final estado = _text(item['estadoInterno']).toUpperCase();
    final Color indicatorColor;
    if (estado == 'CERRADO') {
      indicatorColor = AppTheme.success;
    } else if (estado == 'EN_PROCESO') {
      indicatorColor = AppTheme.warning;
    } else {
      indicatorColor = AppTheme.info;
    }
    return Card(
      child: IntrinsicHeight(
        child: Row(
          children: [
            Container(
              width: 4,
              decoration: BoxDecoration(
                color: indicatorColor,
                borderRadius:
                    const BorderRadius.horizontal(left: Radius.circular(16)),
              ),
            ),
            Expanded(
              child: Padding(
                padding:
                    const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
                child: Row(
                  children: [
                    Container(
                      padding: const EdgeInsets.all(8),
                      decoration: BoxDecoration(
                        color: indicatorColor.withValues(alpha: 0.1),
                        borderRadius: BorderRadius.circular(10),
                      ),
                      child: Icon(Icons.build_circle_outlined,
                          color: indicatorColor, size: 20),
                    ),
                    const SizedBox(width: 12),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            _text(item['equipoCodigoSap'],
                                fallback: 'Sin codigo'),
                            style: const TextStyle(
                                fontWeight: FontWeight.w600, fontSize: 14),
                          ),
                          const SizedBox(height: 2),
                          Text(
                            _text(item['equipoDescripcion']),
                            style: TextStyle(
                                fontSize: 12, color: Colors.grey.shade600),
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                          ),
                        ],
                      ),
                    ),
                    Column(
                      crossAxisAlignment: CrossAxisAlignment.end,
                      mainAxisAlignment: MainAxisAlignment.center,
                      children: [
                        Container(
                          padding: const EdgeInsets.symmetric(
                              horizontal: 8, vertical: 3),
                          decoration: BoxDecoration(
                            color: indicatorColor.withValues(alpha: 0.1),
                            borderRadius: BorderRadius.circular(8),
                          ),
                          child: Text(
                            estado.replaceAll('_', ' '),
                            style: TextStyle(
                                color: indicatorColor,
                                fontSize: 10,
                                fontWeight: FontWeight.w600),
                          ),
                        ),
                        const SizedBox(height: 4),
                        Text(
                          _text(item['fechaMantenimiento'], fallback: '-'),
                          style: TextStyle(
                              fontSize: 11, color: Colors.grey.shade500),
                        ),
                      ],
                    ),
                  ],
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

// ─── ALERT BANNER ──────────────────────────────────────────────────────────

class _AlertBanner extends StatelessWidget {
  const _AlertBanner({
    required this.icon,
    required this.text,
    required this.color,
    required this.bgColor,
  });
  final IconData icon;
  final String text;
  final Color color;
  final Color bgColor;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
        decoration: BoxDecoration(
          color: bgColor,
          borderRadius: BorderRadius.circular(12),
          border: Border.all(color: color.withValues(alpha: 0.3)),
        ),
        child: Row(
          children: [
            Icon(icon, size: 18, color: color),
            const SizedBox(width: 10),
            Expanded(
              child: Text(text, style: TextStyle(color: color, fontSize: 13)),
            ),
          ],
        ),
      ),
    );
  }
}

// ─── MORE TAB ──────────────────────────────────────────────────────────────

class _MoreTab extends StatelessWidget {
  const _MoreTab();

  @override
  Widget build(BuildContext context) {
    final auth = context.watch<AuthProvider>();
    return CustomScrollView(
      physics: const AlwaysScrollableScrollPhysics(),
      slivers: [
        SliverToBoxAdapter(
          child: Container(
            padding: EdgeInsets.only(
              top: MediaQuery.of(context).padding.top + 16,
              left: 20,
              right: 20,
              bottom: 20,
            ),
            decoration: const BoxDecoration(
              gradient: LinearGradient(
                begin: Alignment.topLeft,
                end: Alignment.bottomRight,
                colors: [Color(0xFF2F1857), Color(0xFF47267F)],
              ),
              borderRadius: BorderRadius.vertical(bottom: Radius.circular(24)),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Text('Mas opciones',
                    style: TextStyle(
                        color: Colors.white,
                        fontSize: 22,
                        fontWeight: FontWeight.w700)),
                const SizedBox(height: 4),
                Text('Herramientas adicionales',
                    style: TextStyle(
                        color: Colors.white.withValues(alpha: 0.7),
                        fontSize: 14)),
              ],
            ),
          ),
        ),
        SliverPadding(
          padding: const EdgeInsets.all(16),
          sliver: SliverList(
            delegate: SliverChildListDelegate([
              if (auth.hasCapability(UserCapability.viewVisitas))
                _MoreTile(
                  icon: Icons.assignment_outlined,
                  title: 'Visita tecnica',
                  subtitle: 'Registrar visita en campo',
                  onTap: () => Navigator.of(context).push(
                      MaterialPageRoute(builder: (_) => const VisitasScreen())),
                ),
              if (auth.hasCapability(UserCapability.viewNotificaciones))
                _MoreTile(
                  icon: Icons.notifications_outlined,
                  title: 'Notificaciones',
                  subtitle: 'Alertas y avisos del sistema',
                  onTap: () => Navigator.of(context).push(MaterialPageRoute(
                      builder: (_) => const NotificacionesScreen())),
                ),
              if (auth.hasCapability(UserCapability.manageUbicaciones))
                _MoreTile(
                  icon: Icons.location_city_outlined,
                  title: 'Ubicaciones',
                  subtitle: 'Administrar sedes y oficinas',
                  onTap: () => Navigator.of(context).push(MaterialPageRoute(
                      builder: (_) => const UbicacionesScreen())),
                ),
              if (auth.hasCapability(UserCapability.sendGpsLocation))
                _MoreTile(
                  icon: Icons.gps_fixed,
                  title: 'Monitoreo GPS',
                  subtitle: 'Registro de ubicacion en campo',
                  onTap: () => Navigator.of(context).push(MaterialPageRoute(
                      builder: (_) => const ConsentimientoGpsScreen())),
                ),
              if (auth.hasCapability(UserCapability.viewGpsRealtime))
                _MoreTile(
                  icon: Icons.map_outlined,
                  title: 'Ubicaciones en tiempo real',
                  subtitle: 'Ver tecnicos en campo',
                  onTap: () => Navigator.of(context).push(MaterialPageRoute(
                      builder: (_) => const UbicacionesRealtimeScreen())),
                ),
              _MoreTile(
                icon: Icons.settings_outlined,
                title: 'Ajustes',
                subtitle: 'Configuracion del servidor y app',
                onTap: () => Navigator.of(context).push(
                    MaterialPageRoute(builder: (_) => const SettingsScreen())),
              ),
              const SizedBox(height: 8),
              const Divider(),
              const SizedBox(height: 8),
              _MoreTile(
                icon: Icons.logout_rounded,
                title: 'Cerrar sesion',
                subtitle: auth.session?.username ?? '',
                iconColor: AppTheme.danger,
                onTap: () async {
                  try {
                    await context
                        .read<PushNotificacionService>()
                        .limpiarToken();
                  } catch (_) {}
                  if (context.mounted) context.read<AuthProvider>().logout();
                },
              ),
            ]),
          ),
        ),
      ],
    );
  }
}

class _MoreTile extends StatelessWidget {
  const _MoreTile({
    required this.icon,
    required this.title,
    required this.subtitle,
    required this.onTap,
    this.iconColor,
  });
  final IconData icon;
  final String title;
  final String subtitle;
  final VoidCallback onTap;
  final Color? iconColor;

  @override
  Widget build(BuildContext context) {
    final color = iconColor ?? const Color(0xFF47267F);
    return Card(
      child: ListTile(
        leading: Container(
          padding: const EdgeInsets.all(8),
          decoration: BoxDecoration(
            color: color.withValues(alpha: 0.08),
            borderRadius: BorderRadius.circular(10),
          ),
          child: Icon(icon, color: color, size: 22),
        ),
        title: Text(title, style: const TextStyle(fontWeight: FontWeight.w600)),
        subtitle: Text(subtitle, style: const TextStyle(fontSize: 12)),
        trailing: Icon(Icons.chevron_right, color: Colors.grey.shade400),
        onTap: onTap,
      ),
    );
  }
}

String _text(dynamic value, {String fallback = '-'}) {
  final text = value?.toString().trim() ?? '';
  return text.isEmpty ? fallback : text;
}
