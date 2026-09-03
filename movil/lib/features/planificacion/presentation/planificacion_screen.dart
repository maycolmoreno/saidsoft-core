import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../../core/network/api_client.dart';
import '../../auth/data/auth_models.dart';
import '../../auth/presentation/auth_provider.dart';
import '../data/planificacion_models.dart';
import 'metricas_screen.dart';
import 'planificacion_form_screen.dart';
import 'planificacion_provider.dart';

class PlanificacionScreen extends StatefulWidget {
  const PlanificacionScreen({super.key});

  @override
  State<PlanificacionScreen> createState() => _PlanificacionScreenState();
}

class _PlanificacionScreenState extends State<PlanificacionScreen> {
  late final PlanificacionProvider _provider;

  @override
  void initState() {
    super.initState();
    _provider = PlanificacionProvider(context.read<ApiClient>());
    _loadData();
  }

  Future<void> _loadData() async {
    final auth = context.read<AuthProvider>();
    // Técnicos ven solo sus actividades, admin ve todas
    if (auth.session?.userRole == UserRole.admin) {
      await _provider.cargarActividades();
    } else {
      // Para técnicos, usar su ID
      await _provider.cargarActividades();
    }
  }

  @override
  Widget build(BuildContext context) {
    return ChangeNotifierProvider.value(
      value: _provider,
      child: Consumer<PlanificacionProvider>(
        builder: (context, provider, _) {
          return Scaffold(
            appBar: AppBar(
              title: const Text('Planificación'),
              actions: [
                IconButton(
                  icon: const Icon(Icons.bar_chart),
                  tooltip: 'Métricas',
                  onPressed: () => Navigator.push(
                    context,
                    MaterialPageRoute(
                      builder: (_) => ChangeNotifierProvider.value(
                        value: _provider,
                        child: const MetricasScreen(),
                      ),
                    ),
                  ),
                ),
              ],
            ),
            body: RefreshIndicator(
              onRefresh: _loadData,
              child: Column(
                children: [
                  // Filter chips
                  _FilterBar(
                    selected: provider.filtroEstado,
                    counts: {
                      '': provider.todasActividades.length,
                      'PENDIENTE': provider.totalPendientes,
                      'EN_PROGRESO': provider.totalEnProgreso,
                      'COMPLETADA': provider.totalCompletadas,
                      'VENCIDA': provider.totalVencidas,
                    },
                    onSelected: provider.setFiltroEstado,
                  ),
                  Expanded(
                    child: provider.loading
                        ? const Center(child: CircularProgressIndicator())
                        : provider.error != null
                            ? Center(
                                child: Padding(
                                  padding: const EdgeInsets.all(24),
                                  child: Text(
                                    'Error: ${provider.error}',
                                    textAlign: TextAlign.center,
                                  ),
                                ),
                              )
                            : provider.actividades.isEmpty
                                ? const Center(
                                    child: Padding(
                                      padding: EdgeInsets.all(24),
                                      child: Text(
                                          'No hay actividades planificadas.'),
                                    ),
                                  )
                                : ListView.builder(
                                    physics:
                                        const AlwaysScrollableScrollPhysics(),
                                    padding: const EdgeInsets.all(12),
                                    itemCount: provider.actividades.length,
                                    itemBuilder: (context, index) {
                                      final actividad =
                                          provider.actividades[index];
                                      return _ActividadCard(
                                        actividad: actividad,
                                        onChangeStatus: (estado) async {
                                          await provider.cambiarEstado(
                                            actividad.idActividadPlanificada!,
                                            estado: estado,
                                          );
                                        },
                                      );
                                    },
                                  ),
                  ),
                ],
              ),
            ),
            floatingActionButton: FloatingActionButton.extended(
              onPressed: () async {
                final created = await Navigator.push<bool>(
                  context,
                  MaterialPageRoute(
                    builder: (_) => ChangeNotifierProvider.value(
                      value: _provider,
                      child: const PlanificacionFormScreen(),
                    ),
                  ),
                );
                if (created == true) {
                  await _loadData();
                }
              },
              icon: const Icon(Icons.add),
              label: const Text('Nueva'),
            ),
          );
        },
      ),
    );
  }
}

class _FilterBar extends StatelessWidget {
  const _FilterBar({
    required this.selected,
    required this.counts,
    required this.onSelected,
  });

  final String selected;
  final Map<String, int> counts;
  final ValueChanged<String> onSelected;

  @override
  Widget build(BuildContext context) {
    final filters = {
      '': 'Todas',
      'PENDIENTE': 'Pendiente',
      'EN_PROGRESO': 'En progreso',
      'COMPLETADA': 'Completada',
      'VENCIDA': 'Vencida',
    };
    return SingleChildScrollView(
      scrollDirection: Axis.horizontal,
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
      child: Row(
        children: filters.entries.map((e) {
          final isSelected = selected == e.key;
          final count = counts[e.key] ?? 0;
          return Padding(
            padding: const EdgeInsets.only(right: 8),
            child: FilterChip(
              label: Text('${e.value} ($count)'),
              selected: isSelected,
              onSelected: (_) => onSelected(e.key),
            ),
          );
        }).toList(),
      ),
    );
  }
}

class _ActividadCard extends StatelessWidget {
  const _ActividadCard({
    required this.actividad,
    required this.onChangeStatus,
  });

  final ActividadPlanificada actividad;
  final ValueChanged<String> onChangeStatus;

  Color _estadoColor() {
    switch (actividad.estado) {
      case 'COMPLETADA':
        return const Color(0xFF1F7A3F);
      case 'EN_PROGRESO':
        return const Color(0xFF0C5460);
      case 'VENCIDA':
        return const Color(0xFFB22222);
      case 'CANCELADA':
        return const Color(0xFF383D41);
      default:
        return const Color(0xFF9A5D00);
    }
  }

  Color _prioridadColor() {
    switch (actividad.prioridad) {
      case 'URGENTE':
      case 'ALTA':
        return const Color(0xFFB22222);
      case 'MEDIA':
        return const Color(0xFF9A5D00);
      default:
        return const Color(0xFF1F7A3F);
    }
  }

  IconData _tipoIcon() {
    switch (actividad.tipoActividad) {
      case 'TAREA_DIARIA':
        return Icons.today;
      case 'TAREA_SEMANAL':
        return Icons.date_range;
      case 'MANTENIMIENTO_PROGRAMADO':
        return Icons.build;
      case 'VISITA_TECNICA':
        return Icons.location_on;
      case 'OBJETIVO_MENSUAL':
        return Icons.flag;
      default:
        return Icons.task;
    }
  }

  @override
  Widget build(BuildContext context) {
    return Card(
      margin: const EdgeInsets.only(bottom: 10),
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(_tipoIcon(), size: 20, color: _estadoColor()),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    actividad.titulo,
                    style: const TextStyle(
                        fontWeight: FontWeight.bold, fontSize: 15),
                  ),
                ),
                Container(
                  padding:
                      const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                  decoration: BoxDecoration(
                    color: _estadoColor().withValues(alpha: 0.1),
                    borderRadius: BorderRadius.circular(12),
                  ),
                  child: Text(
                    actividad.estadoLabel,
                    style: TextStyle(
                      fontSize: 11,
                      fontWeight: FontWeight.w700,
                      color: _estadoColor(),
                    ),
                  ),
                ),
              ],
            ),
            const SizedBox(height: 6),
            Row(
              children: [
                _InfoChip(
                    icon: Icons.person, label: actividad.tecnicoNombre ?? '-'),
                const SizedBox(width: 8),
                _InfoChip(
                  icon: Icons.priority_high,
                  label: actividad.prioridad,
                  color: _prioridadColor(),
                ),
              ],
            ),
            const SizedBox(height: 4),
            Row(
              children: [
                _InfoChip(
                    icon: Icons.calendar_today,
                    label: '${actividad.fechaInicio} → ${actividad.fechaFin}'),
              ],
            ),
            if (actividad.descripcion != null &&
                actividad.descripcion!.isNotEmpty) ...[
              const SizedBox(height: 4),
              Text(
                actividad.descripcion!,
                style: const TextStyle(fontSize: 13, color: Color(0xFF6D7A92)),
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
              ),
            ],
            if (!actividad.isCompletada && !actividad.isVencida) ...[
              const SizedBox(height: 8),
              Row(
                mainAxisAlignment: MainAxisAlignment.end,
                children: [
                  if (actividad.isPendiente)
                    TextButton.icon(
                      onPressed: () => onChangeStatus('EN_PROGRESO'),
                      icon: const Icon(Icons.play_arrow, size: 18),
                      label: const Text('Iniciar'),
                      style: TextButton.styleFrom(
                          visualDensity: VisualDensity.compact),
                    ),
                  if (actividad.isPendiente || actividad.isEnProgreso)
                    TextButton.icon(
                      onPressed: () => onChangeStatus('COMPLETADA'),
                      icon: const Icon(Icons.check, size: 18),
                      label: const Text('Completar'),
                      style: TextButton.styleFrom(
                        foregroundColor: const Color(0xFF1F7A3F),
                        visualDensity: VisualDensity.compact,
                      ),
                    ),
                ],
              ),
            ],
          ],
        ),
      ),
    );
  }
}

class _InfoChip extends StatelessWidget {
  const _InfoChip({required this.icon, required this.label, this.color});

  final IconData icon;
  final String label;
  final Color? color;

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Icon(icon, size: 14, color: color ?? const Color(0xFF6D7A92)),
        const SizedBox(width: 4),
        Text(
          label,
          style: TextStyle(
            fontSize: 12,
            color: color ?? const Color(0xFF6D7A92),
            fontWeight: color != null ? FontWeight.w600 : FontWeight.normal,
          ),
        ),
      ],
    );
  }
}
