import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../../core/network/api_client.dart';
import '../../auth/data/auth_models.dart';
import '../../auth/presentation/auth_provider.dart';
import '../../mantenimientos/presentation/mantenimiento_form_screen.dart';
import '../data/equipo_models.dart';
import '../data/equipos_repository.dart';

class EquipoDetailScreen extends StatefulWidget {
  const EquipoDetailScreen({
    super.key,
    required this.equipoId,
  });

  final int equipoId;

  @override
  State<EquipoDetailScreen> createState() => _EquipoDetailScreenState();
}

class _EquipoDetailScreenState extends State<EquipoDetailScreen> {
  late Future<EquipoHistorial> _future;

  @override
  void initState() {
    super.initState();
    _future = _load();
  }

  Future<EquipoHistorial> _load() {
    return EquiposRepository(context.read<ApiClient>())
        .obtenerDetalle(widget.equipoId);
  }

  Future<void> _reload() async {
    final future = _load();
    setState(() => _future = future);
    await future;
  }

  @override
  Widget build(BuildContext context) {
    final canCreateMantenimiento = context.watch<AuthProvider>().hasCapability(
          UserCapability.createMantenimiento,
        );
    return Scaffold(
      appBar: AppBar(title: const Text('Detalle del equipo')),
      body: FutureBuilder<EquipoHistorial>(
        future: _future,
        builder: (context, snapshot) {
          if (snapshot.connectionState == ConnectionState.waiting) {
            return const Center(child: CircularProgressIndicator());
          }
          if (snapshot.hasError) {
            return _RetryState(
              message: 'No fue posible cargar el historial del equipo.',
              onRetry: _reload,
            );
          }

          final data = snapshot.data;
          if (data == null) {
            return _RetryState(
              message: 'No hay informacion disponible para este equipo.',
              onRetry: _reload,
            );
          }

          final equipo = data.equipo;
          final estadisticas = data.estadisticas;
          final mantenimientos = data.mantenimientos;

          return RefreshIndicator(
            onRefresh: _reload,
            child: ListView(
              padding: const EdgeInsets.all(16),
              physics: const AlwaysScrollableScrollPhysics(),
              children: [
                _SectionCard(
                  title: equipo.codigoSap,
                  rows: [
                    _row('Marca', equipo.marca),
                    _row('Modelo', equipo.modelo),
                    _row('Serial', equipo.serial),
                    _row('Estado', equipo.estadoEquipo),
                    _row('Categoria', equipo.categoriaNombre),
                    _row('Procesador', equipo.procesador),
                    _row('RAM', _gb(equipo.memoriaRamGb)),
                    _row('Disco', _gb(equipo.capacidadAlmacenamientoGb)),
                    _row('MAC', equipo.mac),
                    _row('Licencia Windows',
                        _boolLabel(equipo.licenciaWindowsActivada)),
                    _row('Fecha compra', equipo.fechaCompra),
                    _row('Observacion', equipo.observacionEquipo),
                  ],
                ),
                const SizedBox(height: 12),
                if (canCreateMantenimiento) ...[
                  FilledButton.icon(
                    onPressed: () async {
                      final result = await Navigator.of(context).push<bool>(
                        MaterialPageRoute(
                          builder: (_) => MantenimientoFormScreen(
                            initialEquipoIds: [widget.equipoId],
                          ),
                        ),
                      );
                      if (result == true && context.mounted) {
                        ScaffoldMessenger.of(context).showSnackBar(
                          const SnackBar(
                            content:
                                Text('Mantenimiento creado desde el equipo.'),
                          ),
                        );
                        await _reload();
                      }
                    },
                    icon: const Icon(Icons.build_outlined),
                    label: const Text('Nuevo mantenimiento'),
                  ),
                  const SizedBox(height: 12),
                ],
                _SectionCard(
                  title: 'Custodia y ubicacion',
                  rows: [
                    _row('Custodio', equipo.custodioNombre),
                    _row('Departamento', equipo.departamentoNombre),
                    _row('Ubicacion', equipo.ubicacionNombre),
                    _row('Ciudad', equipo.ubicacionCiudad),
                    _row('Desde', equipo.fechaInicioCustodio),
                  ],
                ),
                const SizedBox(height: 12),
                _SectionCard(
                  title: 'Estadisticas',
                  rows: [
                    _row('Estado mantenimiento', data.estadoMantenimiento),
                    _row('Total mantenimientos',
                        estadisticas.totalMantenimientos),
                    _row('Cerrados', estadisticas.totalCerrados),
                    _row('En proceso', estadisticas.totalEnProceso),
                    _row('Dias sin mantenimiento',
                        estadisticas.diasSinMantenimiento),
                    _row(
                      'Promedio entre mantenimientos',
                      estadisticas.promedioDiasEntreMantenimientos,
                    ),
                  ],
                ),
                const SizedBox(height: 12),
                Text(
                  'Historial de mantenimientos',
                  style: Theme.of(context).textTheme.titleMedium,
                ),
                const SizedBox(height: 8),
                if (mantenimientos.isEmpty)
                  const Card(
                    child: Padding(
                      padding: EdgeInsets.all(16),
                      child: Text('No hay mantenimientos registrados.'),
                    ),
                  )
                else
                  ...mantenimientos.map(
                    (item) => Card(
                      child: ListTile(
                        leading: const Icon(Icons.build_outlined),
                        title: Text(_text(item.tipoInferido)),
                        subtitle: Text(
                          '${_text(item.descripcion)}\n'
                          'Tecnico: ${_text(item.tecnicoNombre)}\n'
                          'Estado: ${_text(item.estadoInterno)}',
                        ),
                        isThreeLine: true,
                        trailing: Text(_text(item.fechaCierre, fallback: '-')),
                      ),
                    ),
                  ),
              ],
            ),
          );
        },
      ),
    );
  }
}

class _SectionCard extends StatelessWidget {
  const _SectionCard({
    required this.title,
    required this.rows,
  });

  final String title;
  final List<MapEntry<String, String>> rows;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(title, style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 12),
            ...rows
                .where((row) => row.value.isNotEmpty && row.value != '-')
                .map(
                  (row) => Padding(
                    padding: const EdgeInsets.only(bottom: 8),
                    child: Row(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        SizedBox(
                          width: 120,
                          child: Text(
                            row.key,
                            style: const TextStyle(fontWeight: FontWeight.w600),
                          ),
                        ),
                        Expanded(child: Text(row.value)),
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

class _RetryState extends StatelessWidget {
  const _RetryState({
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
          OutlinedButton(onPressed: onRetry, child: const Text('Reintentar')),
        ],
      ),
    );
  }
}

MapEntry<String, String> _row(String label, dynamic value) {
  return MapEntry(label, _text(value));
}

String _gb(dynamic value) {
  final text = _text(value, fallback: '');
  return text.isEmpty ? '' : '$text GB';
}

String _boolLabel(bool? value) {
  return value == true
      ? 'Si'
      : value == false
          ? 'No'
          : '-';
}

String _text(dynamic value, {String fallback = '-'}) {
  final text = value?.toString().trim() ?? '';
  return text.isEmpty ? fallback : text;
}
