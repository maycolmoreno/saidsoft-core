import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../auth/presentation/auth_provider.dart';
import 'planificacion_provider.dart';

class MetricasScreen extends StatefulWidget {
  const MetricasScreen({super.key});

  @override
  State<MetricasScreen> createState() => _MetricasScreenState();
}

class _MetricasScreenState extends State<MetricasScreen> {
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) => _loadMetrics());
  }

  Future<void> _loadMetrics() async {
    final provider = context.read<PlanificacionProvider>();
    final auth = context.read<AuthProvider>();
    final tecnicoId = auth.userId ?? 1;
    await provider.cargarMetricas(tecnicoId);
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Métricas de Cumplimiento')),
      body: Consumer<PlanificacionProvider>(
        builder: (context, provider, _) {
          if (provider.loading) {
            return const Center(child: CircularProgressIndicator());
          }
          if (provider.error != null) {
            return Center(
              child: Padding(
                padding: const EdgeInsets.all(24),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Text('Error: ${provider.error}'),
                    const SizedBox(height: 12),
                    ElevatedButton(
                      onPressed: _loadMetrics,
                      child: const Text('Reintentar'),
                    ),
                  ],
                ),
              ),
            );
          }

          final metricas = provider.metricas;
          if (metricas == null) {
            return const Center(child: Text('No hay datos disponibles.'));
          }

          return RefreshIndicator(
            onRefresh: _loadMetrics,
            child: ListView(
              physics: const AlwaysScrollableScrollPhysics(),
              padding: const EdgeInsets.all(16),
              children: [
                // Period selector
                _PeriodSelector(
                  selected: provider.periodoMetricas,
                  onChanged: (periodo) {
                    provider.setPeriodoMetricas(periodo);
                    _loadMetrics();
                  },
                ),
                const SizedBox(height: 16),

                // Main percentage card
                _PercentageCard(
                  title: 'Tareas completadas',
                  percentage: metricas.porcentajeCompletadas,
                  subtitle:
                      '${metricas.completadas} de ${metricas.totalActividades}',
                ),
                const SizedBox(height: 12),

                _PercentageCard(
                  title: 'Cumplimiento a tiempo',
                  percentage: metricas.porcentajeCumplimientoATiempo,
                  subtitle:
                      '${metricas.completadasATiempo} a tiempo, ${metricas.completadasTarde} tarde',
                ),
                const SizedBox(height: 16),

                // Stats grid
                Wrap(
                  spacing: 10,
                  runSpacing: 10,
                  children: [
                    _StatTile(
                      label: 'Pendientes',
                      value: '${metricas.pendientes}',
                      color: const Color(0xFF9A5D00),
                    ),
                    _StatTile(
                      label: 'En progreso',
                      value: '${metricas.enProgreso}',
                      color: const Color(0xFF0C5460),
                    ),
                    _StatTile(
                      label: 'Vencidas',
                      value: '${metricas.vencidas}',
                      color: const Color(0xFFB22222),
                    ),
                    _StatTile(
                      label: 'Canceladas',
                      value: '${metricas.canceladas}',
                      color: const Color(0xFF383D41),
                    ),
                    _StatTile(
                      label: 'Tiempo promedio',
                      value:
                          '${metricas.tiempoPromedioMinutos.toStringAsFixed(0)} min',
                      color: const Color(0xFF4E4ACB),
                    ),
                    _StatTile(
                      label: 'Total',
                      value: '${metricas.totalActividades}',
                      color: const Color(0xFF152238),
                    ),
                  ],
                ),
              ],
            ),
          );
        },
      ),
    );
  }
}

class _PeriodSelector extends StatelessWidget {
  const _PeriodSelector({required this.selected, required this.onChanged});

  final String selected;
  final ValueChanged<String> onChanged;

  @override
  Widget build(BuildContext context) {
    return SegmentedButton<String>(
      segments: const [
        ButtonSegment(value: 'SEMANAL', label: Text('Semanal')),
        ButtonSegment(value: 'MENSUAL', label: Text('Mensual')),
        ButtonSegment(value: 'GLOBAL', label: Text('Global')),
      ],
      selected: {selected},
      onSelectionChanged: (s) => onChanged(s.first),
    );
  }
}

class _PercentageCard extends StatelessWidget {
  const _PercentageCard({
    required this.title,
    required this.percentage,
    required this.subtitle,
  });

  final String title;
  final double percentage;
  final String subtitle;

  Color get _color {
    if (percentage >= 80) return const Color(0xFF1F7A3F);
    if (percentage >= 50) return const Color(0xFF9A5D00);
    return const Color(0xFFB22222);
  }

  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(title,
                style:
                    const TextStyle(fontWeight: FontWeight.w600, fontSize: 14)),
            const SizedBox(height: 8),
            Row(
              children: [
                Expanded(
                  child: ClipRRect(
                    borderRadius: BorderRadius.circular(4),
                    child: LinearProgressIndicator(
                      value: percentage / 100,
                      minHeight: 10,
                      backgroundColor: const Color(0xFFF0F1F5),
                      color: _color,
                    ),
                  ),
                ),
                const SizedBox(width: 12),
                Text(
                  '${percentage.toStringAsFixed(1)}%',
                  style: TextStyle(
                    fontWeight: FontWeight.bold,
                    fontSize: 18,
                    color: _color,
                  ),
                ),
              ],
            ),
            const SizedBox(height: 4),
            Text(subtitle,
                style: const TextStyle(fontSize: 12, color: Color(0xFF6D7A92))),
          ],
        ),
      ),
    );
  }
}

class _StatTile extends StatelessWidget {
  const _StatTile({
    required this.label,
    required this.value,
    required this.color,
  });

  final String label;
  final String value;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      width: (MediaQuery.of(context).size.width - 54) / 3,
      child: Card(
        child: Padding(
          padding: const EdgeInsets.symmetric(vertical: 14, horizontal: 10),
          child: Column(
            children: [
              Text(
                value,
                style: TextStyle(
                  fontSize: 22,
                  fontWeight: FontWeight.w800,
                  color: color,
                ),
              ),
              const SizedBox(height: 4),
              Text(
                label,
                style: const TextStyle(fontSize: 11, color: Color(0xFF6D7A92)),
                textAlign: TextAlign.center,
              ),
            ],
          ),
        ),
      ),
    );
  }
}
