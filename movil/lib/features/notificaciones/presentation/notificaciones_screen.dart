import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../../core/network/api_client.dart';
import '../data/notificacion_model.dart';
import '../data/notificaciones_repository.dart';

class NotificacionesScreen extends StatefulWidget {
  const NotificacionesScreen({super.key});

  @override
  State<NotificacionesScreen> createState() => _NotificacionesScreenState();
}

class _NotificacionesScreenState extends State<NotificacionesScreen> {
  String _filtro = 'todas';
  late Future<List<Notificacion>> _future;

  @override
  void initState() {
    super.initState();
    _future = _loadNotificaciones();
  }

  Future<List<Notificacion>> _loadNotificaciones() async {
    return NotificacionesRepository(context.read<ApiClient>()).listar();
  }

  Future<void> _reload() async {
    final future = _loadNotificaciones();
    setState(() => _future = future);
    await future;
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Notificaciones')),
      body: Column(
        children: [
          DropdownButtonFormField<String>(
            initialValue: _filtro,
            decoration: const InputDecoration(labelText: 'Filtro'),
            items: const [
              DropdownMenuItem(value: 'todas', child: Text('Todas')),
              DropdownMenuItem(value: 'nuevas', child: Text('No leidas')),
              DropdownMenuItem(value: 'leidas', child: Text('Leidas')),
            ],
            onChanged: (value) => setState(() => _filtro = value ?? 'todas'),
          ),
          const SizedBox(height: 16),
          Expanded(
            child: FutureBuilder<List<Notificacion>>(
              future: _future,
              builder: (context, snapshot) {
                if (snapshot.connectionState == ConnectionState.waiting) {
                  return const Center(child: CircularProgressIndicator());
                }
                if (snapshot.hasError) {
                  return _ErrorState(
                    message: 'No fue posible cargar las notificaciones.',
                    onRetry: _reload,
                  );
                }
                final items = _filter(snapshot.data ?? const []);
                if (items.isEmpty) {
                  return RefreshIndicator(
                    onRefresh: _reload,
                    child: ListView(
                      padding: const EdgeInsets.all(16),
                      physics: const AlwaysScrollableScrollPhysics(),
                      children: const [
                        SizedBox(height: 80),
                        Center(
                            child: Text('Sin notificaciones para este filtro')),
                      ],
                    ),
                  );
                }

                return RefreshIndicator(
                  onRefresh: _reload,
                  child: ListView.separated(
                    padding: const EdgeInsets.all(16),
                    physics: const AlwaysScrollableScrollPhysics(),
                    itemCount: items.length,
                    separatorBuilder: (context, index) =>
                        const SizedBox(height: 12),
                    itemBuilder: (context, index) {
                      final item = items[index];
                      final leida = item.leida;
                      return Card(
                        child: ListTile(
                          onTap: () async {
                            if (item.id <= 0 || leida) {
                              return;
                            }
                            await NotificacionesRepository(
                                    context.read<ApiClient>())
                                .marcarLeida(item.id);
                            if (!mounted) return;
                            ScaffoldMessenger.of(this.context).showSnackBar(
                              const SnackBar(
                                content:
                                    Text('Notificacion marcada como leida'),
                              ),
                            );
                            await _reload();
                          },
                          leading: Icon(
                            leida
                                ? Icons.drafts_outlined
                                : Icons.notifications_active_outlined,
                            color: leida ? Colors.grey : Colors.orange,
                          ),
                          title: Text(item.mensaje),
                          subtitle: Text(
                            'Mantenimiento: ${item.referenciaMantenimientoId}\n${item.creadoEn}',
                          ),
                          isThreeLine: true,
                          trailing: _Badge(
                            text: leida ? 'Leida' : 'Nueva',
                            color: leida ? Colors.blueGrey : Colors.orange,
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
      ),
    );
  }

  List<Notificacion> _filter(List<Notificacion> items) {
    return items.where((item) {
      final leida = item.leida;
      if (_filtro == 'nuevas') {
        return !leida;
      }
      if (_filtro == 'leidas') {
        return leida;
      }
      return true;
    }).toList();
  }
}

class _Badge extends StatelessWidget {
  const _Badge({
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
