import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../../core/network/api_client.dart';
import '../../auth/data/auth_models.dart';
import '../../auth/presentation/auth_provider.dart';
import '../data/mantenimientos_repository.dart';

class MantenimientoDetailScreen extends StatefulWidget {
  const MantenimientoDetailScreen({
    super.key,
    required this.mantenimientoId,
  });

  final int mantenimientoId;

  @override
  State<MantenimientoDetailScreen> createState() =>
      _MantenimientoDetailScreenState();
}

class _MantenimientoDetailScreenState extends State<MantenimientoDetailScreen> {
  final _observacionesController = TextEditingController();
  late Future<Map<String, dynamic>> _future;
  bool _closing = false;

  @override
  void initState() {
    super.initState();
    _future = _load();
  }

  @override
  void dispose() {
    _observacionesController.dispose();
    super.dispose();
  }

  Future<Map<String, dynamic>> _load() {
    return MantenimientosRepository(context.read<ApiClient>())
        .obtenerDetalle(widget.mantenimientoId);
  }

  Future<void> _reload() async {
    final future = _load();
    setState(() {
      _future = future;
    });
    await future;
  }

  Future<void> _cerrar() async {
    final observaciones = _observacionesController.text.trim();
    if (observaciones.isEmpty) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
            content:
                Text('Ingresa una descripcion para cerrar el mantenimiento.')),
      );
      return;
    }

    setState(() => _closing = true);
    try {
      final queuedOffline =
          await MantenimientosRepository(context.read<ApiClient>())
              .cerrarConFallback(
        mantenimientoId: widget.mantenimientoId,
        observaciones: observaciones,
      );
      _observacionesController.clear();
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            queuedOffline
                ? 'Sin conexion. El cierre quedo pendiente de sincronizacion.'
                : 'Mantenimiento cerrado correctamente.',
          ),
        ),
      );
      if (!queuedOffline) {
        await _reload();
      }
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
            content: Text(
                'Error al cerrar: ${e.toString().replaceAll('Exception: ', '')}')),
      );
    } finally {
      if (mounted) {
        setState(() => _closing = false);
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final canClose = context.watch<AuthProvider>().hasCapability(
          UserCapability.closeMantenimiento,
        );
    return Scaffold(
      appBar: AppBar(title: const Text('Detalle del mantenimiento')),
      body: FutureBuilder<Map<String, dynamic>>(
        future: _future,
        builder: (context, snapshot) {
          if (snapshot.connectionState == ConnectionState.waiting) {
            return const Center(child: CircularProgressIndicator());
          }
          if (snapshot.hasError) {
            return _RetryState(
              message: 'No fue posible cargar el mantenimiento.',
              onRetry: _reload,
            );
          }

          final item = snapshot.data ?? const {};
          final cerrado =
              _text(item['estadoInterno']).toUpperCase() == 'CERRADO';

          return RefreshIndicator(
            onRefresh: _reload,
            child: ListView(
              padding: const EdgeInsets.all(16),
              physics: const AlwaysScrollableScrollPhysics(),
              children: [
                Card(
                  child: Padding(
                    padding: const EdgeInsets.all(16),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          _text(item['equipoCodigoSap'],
                              fallback: 'Sin codigo'),
                          style: Theme.of(context).textTheme.titleMedium,
                        ),
                        const SizedBox(height: 12),
                        _line('Equipo', item['equipoDescripcion']),
                        _line('Tecnico', item['tecnicoNombre']),
                        _line('Estado', item['estadoInterno']),
                        _line('Fecha', item['fechaMantenimiento']),
                        _line('SINE', item['sineSnapshoted']),
                        _line('Ticket', item['ticketId']),
                        _line('Descripcion', item['descripcion']),
                        _line('Trabajo realizado',
                            item['descripcionTrabajoRealizado']),
                      ],
                    ),
                  ),
                ),
                const SizedBox(height: 12),
                if (!cerrado && canClose) ...[
                  TextField(
                    controller: _observacionesController,
                    minLines: 3,
                    maxLines: 5,
                    decoration: const InputDecoration(
                      labelText: 'Observaciones de cierre',
                      hintText: 'Describe el trabajo realizado por el tecnico',
                    ),
                  ),
                  const SizedBox(height: 12),
                  FilledButton.icon(
                    onPressed: _closing ? null : _cerrar,
                    icon: _closing
                        ? const SizedBox(
                            height: 16,
                            width: 16,
                            child: CircularProgressIndicator(strokeWidth: 2),
                          )
                        : const Icon(Icons.check_circle_outline),
                    label:
                        Text(_closing ? 'Cerrando...' : 'Cerrar mantenimiento'),
                  ),
                ] else if (cerrado)
                  const Card(
                    child: Padding(
                      padding: EdgeInsets.all(16),
                      child: Text('Este mantenimiento ya fue cerrado.'),
                    ),
                  )
                else
                  const Card(
                    child: Padding(
                      padding: EdgeInsets.all(16),
                      child: Text(
                          'Tu rol no puede cerrar mantenimientos desde la app.'),
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

Widget _line(String label, dynamic value) {
  final text = _text(value, fallback: '');
  if (text.isEmpty) {
    return const SizedBox.shrink();
  }
  return Padding(
    padding: const EdgeInsets.only(bottom: 8),
    child: RichText(
      text: TextSpan(
        style: const TextStyle(color: Colors.black87),
        children: [
          TextSpan(
            text: '$label: ',
            style: const TextStyle(fontWeight: FontWeight.w700),
          ),
          TextSpan(text: text),
        ],
      ),
    ),
  );
}

String _text(dynamic value, {String fallback = '-'}) {
  final text = value?.toString().trim() ?? '';
  return text.isEmpty ? fallback : text;
}
