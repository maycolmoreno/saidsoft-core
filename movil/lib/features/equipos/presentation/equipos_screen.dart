import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../../core/network/api_client.dart';
import '../data/equipo_models.dart';
import '../data/equipos_repository.dart';
import 'equipo_detail_screen.dart';

class EquiposScreen extends StatefulWidget {
  const EquiposScreen({super.key});

  @override
  State<EquiposScreen> createState() => _EquiposScreenState();
}

class _EquiposScreenState extends State<EquiposScreen> {
  final _searchController = TextEditingController();
  String _estado = 'todos';
  String _custodio = 'todos';
  String _ubicacion = 'todos';
  late Future<List<EquipoListItem>> _future;

  @override
  void initState() {
    super.initState();
    _future = _loadEquipos();
  }

  @override
  void dispose() {
    _searchController.dispose();
    super.dispose();
  }

  Future<List<EquipoListItem>> _loadEquipos() async {
    return EquiposRepository(context.read<ApiClient>()).listarConHistorial();
  }

  Future<void> _refresh() async {
    final future = _loadEquipos();
    setState(() => _future = future);
    await future;
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        TextField(
          controller: _searchController,
          onChanged: (_) => setState(() {}),
          decoration: const InputDecoration(
            hintText: 'Buscar por serial, modelo o codigo',
            prefixIcon: Icon(Icons.search),
          ),
        ),
        const SizedBox(height: 16),
        Row(
          children: [
            Expanded(
              child: DropdownButtonFormField<String>(
                initialValue: _estado,
                decoration: const InputDecoration(labelText: 'Estado'),
                items: const [
                  DropdownMenuItem(value: 'todos', child: Text('Todos')),
                  DropdownMenuItem(value: 'ACTIVO', child: Text('Activos')),
                  DropdownMenuItem(value: 'BAJA', child: Text('Baja')),
                ],
                onChanged: (value) =>
                    setState(() => _estado = value ?? 'todos'),
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: FutureBuilder<List<EquipoListItem>>(
                future: _future,
                builder: (context, snapshot) {
                  final custodios = _custodios(snapshot.data ?? const []);
                  return DropdownButtonFormField<String>(
                    initialValue: _custodio,
                    decoration: const InputDecoration(labelText: 'Custodio'),
                    items: [
                      const DropdownMenuItem(
                          value: 'todos', child: Text('Todos')),
                      ...custodios.map(
                        (custodio) => DropdownMenuItem(
                          value: custodio,
                          child: Text(custodio),
                        ),
                      ),
                    ],
                    onChanged: (value) =>
                        setState(() => _custodio = value ?? 'todos'),
                  );
                },
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: FutureBuilder<List<EquipoListItem>>(
                future: _future,
                builder: (context, snapshot) {
                  final ubicaciones = _ubicaciones(snapshot.data ?? const []);
                  return DropdownButtonFormField<String>(
                    initialValue: _ubicacion,
                    decoration: const InputDecoration(labelText: 'Ubicacion'),
                    items: [
                      const DropdownMenuItem(
                          value: 'todos', child: Text('Todas')),
                      ...ubicaciones.map(
                        (ubicacion) => DropdownMenuItem(
                          value: ubicacion,
                          child: Text(ubicacion),
                        ),
                      ),
                    ],
                    onChanged: (value) =>
                        setState(() => _ubicacion = value ?? 'todos'),
                  );
                },
              ),
            ),
          ],
        ),
        const SizedBox(height: 16),
        Expanded(
          child: FutureBuilder<List<EquipoListItem>>(
            future: _future,
            builder: (context, snapshot) {
              if (snapshot.connectionState == ConnectionState.waiting) {
                return const Center(child: CircularProgressIndicator());
              }
              if (snapshot.hasError) {
                return _ErrorState(
                  message: 'No fue posible cargar los equipos.',
                  onRetry: _refresh,
                );
              }

              final equipos = _filter(snapshot.data ?? const []);
              if (equipos.isEmpty) {
                return RefreshIndicator(
                  onRefresh: _refresh,
                  child: ListView(
                    physics: const AlwaysScrollableScrollPhysics(),
                    children: const [
                      SizedBox(height: 80),
                      Center(child: Text('No hay equipos para mostrar.')),
                    ],
                  ),
                );
              }

              return RefreshIndicator(
                onRefresh: _refresh,
                child: ListView.separated(
                  physics: const AlwaysScrollableScrollPhysics(),
                  itemCount: equipos.length,
                  separatorBuilder: (context, index) =>
                      const SizedBox(height: 12),
                  itemBuilder: (context, index) {
                    final equipo = equipos[index];
                    return Card(
                      child: ListTile(
                        onTap: equipo.id == 0
                            ? null
                            : () {
                                Navigator.of(context).push(
                                  MaterialPageRoute(
                                    builder: (_) =>
                                        EquipoDetailScreen(equipoId: equipo.id),
                                  ),
                                );
                              },
                        leading: const Icon(Icons.computer_outlined),
                        title: Text(equipo.codigoSap),
                        subtitle: Text(
                          [
                            _text(equipo.modelo),
                            'Serial: ${_text(equipo.serial)}',
                            'Custodio: ${_text(equipo.custodioNombre, fallback: 'Sin custodio')}',
                            'Ubicacion: ${_text(equipo.ubicacionNombre, fallback: 'Sin ubicacion')}',
                          ].where((part) => part.isNotEmpty).join('\n'),
                        ),
                        isThreeLine: true,
                        trailing: Column(
                          mainAxisAlignment: MainAxisAlignment.center,
                          crossAxisAlignment: CrossAxisAlignment.end,
                          children: [
                            _StatusChip(
                              text: _text(equipo.estadoEquipo, fallback: '-'),
                              color: _estadoColor(equipo.estadoEquipo),
                            ),
                            const SizedBox(height: 6),
                            _StatusChip(
                              text: _mantenimientoResumen(equipo),
                              color: _mantenimientoColor(
                                equipo.estadoMantenimiento,
                                equipo.diasSinMantenimiento,
                              ),
                            ),
                            const SizedBox(height: 6),
                            if (_sinMantenimiento(equipo))
                              const _StatusChip(
                                text: 'Sin mtto',
                                color: Colors.red,
                              )
                            else if (_mantenimientoVencido(equipo))
                              const _StatusChip(
                                text: 'Mtto vencido',
                                color: Colors.deepOrange,
                              ),
                          ],
                        ),
                      ),
                    );
                  },
                ),
              );
            },
          ),
        ),
      ],
    );
  }

  List<EquipoListItem> _filter(List<EquipoListItem> items) {
    final query = _searchController.text.trim().toLowerCase();
    return items.where((item) {
      final estadoOk =
          _estado == 'todos' || item.estadoEquipo.toUpperCase() == _estado;
      final custodioOk = _custodio == 'todos' ||
          item.custodioNombre.toUpperCase() == _custodio.toUpperCase();
      final ubicacionOk = _ubicacion == 'todos' ||
          item.ubicacionNombre.toUpperCase() == _ubicacion.toUpperCase();
      final haystack = [
        _text(item.codigoSap),
        _text(item.serial),
        _text(item.modelo),
        _text(item.procesador),
        _text(item.mac),
        _text(item.custodioNombre),
        _text(item.ubicacionNombre),
      ].join(' ').toLowerCase();
      final searchOk = query.isEmpty || haystack.contains(query);
      return estadoOk && custodioOk && ubicacionOk && searchOk;
    }).toList();
  }

  List<String> _custodios(List<EquipoListItem> items) {
    final custodios = items
        .map((item) => _text(item.custodioNombre, fallback: ''))
        .where((item) => item.isNotEmpty)
        .toSet()
        .toList();
    custodios.sort();
    return custodios;
  }

  List<String> _ubicaciones(List<EquipoListItem> items) {
    final ubicaciones = items
        .map((item) => _text(item.ubicacionNombre, fallback: ''))
        .where((item) => item.isNotEmpty)
        .toSet()
        .toList();
    ubicaciones.sort();
    return ubicaciones;
  }
}

class _ErrorState extends StatelessWidget {
  const _ErrorState({
    required this.message,
    required this.onRetry,
  });

  final String message;
  final Future<void> Function() onRetry;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Text(message),
          const SizedBox(height: 12),
          OutlinedButton(
            onPressed: onRetry,
            child: const Text('Reintentar'),
          ),
        ],
      ),
    );
  }
}

class _StatusChip extends StatelessWidget {
  const _StatusChip({
    required this.text,
    required this.color,
  });

  final String text;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(999),
      ),
      child: Text(
        text,
        style: TextStyle(
          color: color,
          fontSize: 11,
          fontWeight: FontWeight.w700,
        ),
      ),
    );
  }
}

String _text(dynamic value, {String fallback = '-'}) {
  final text = value?.toString().trim() ?? '';
  return text.isEmpty ? fallback : text;
}

String _mantenimientoResumen(EquipoListItem item) {
  final estado = _text(item.estadoMantenimiento, fallback: '');
  final dias = item.diasSinMantenimiento;
  if (estado.isEmpty || estado == '-') {
    return dias > 0 ? '$dias dias sin mtto' : 'Sin historial';
  }
  return '$estado · $dias d';
}

bool _sinMantenimiento(EquipoListItem item) {
  final estado = _text(item.estadoMantenimiento, fallback: '');
  final dias = item.diasSinMantenimiento;
  return (estado.isEmpty || estado == '-') && dias == 0;
}

bool _mantenimientoVencido(EquipoListItem item) {
  final estado = _text(item.estadoMantenimiento).toUpperCase();
  final dias = item.diasSinMantenimiento;
  return estado.contains('VENC') || dias >= 180;
}

Color _estadoColor(String estado) {
  switch (estado.toUpperCase()) {
    case 'NO_OPERATIVO':
    case 'BAJA':
      return Colors.red;
    case 'REQUIERE_REVISION':
      return Colors.orange;
    default:
      return Colors.green;
  }
}

Color _mantenimientoColor(String estado, int dias) {
  final normalized = estado.toUpperCase();
  if (normalized.contains('VENC') || dias >= 180) {
    return Colors.red;
  }
  if (normalized.contains('PROX') || dias >= 90) {
    return Colors.orange;
  }
  return Colors.green;
}
