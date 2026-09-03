import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../../core/config/app_config.dart';
import '../../../core/network/api_client.dart';
import '../../../core/storage/local_database.dart';
import '../../auth/data/auth_models.dart';
import '../../auth/presentation/auth_provider.dart';
import '../../notificaciones/data/push_notificacion_service.dart';
import '../../sync/sync_service.dart';
import '../../ubicaciones/presentation/ubicaciones_screen.dart';
import 'server_config_screen.dart';

class SettingsScreen extends StatefulWidget {
  const SettingsScreen({super.key});

  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> {
  bool _syncing = false;
  late Future<int> _pendingFuture;

  @override
  void initState() {
    super.initState();
    _pendingFuture = LocalDatabase.instance.contarPendientes();
  }

  Future<void> _reloadPending() async {
    final future = LocalDatabase.instance.contarPendientes();
    setState(() => _pendingFuture = future);
    await future;
  }

  Future<void> _syncNow() async {
    final messenger = ScaffoldMessenger.of(context);
    setState(() => _syncing = true);
    try {
      final synced =
          await SyncService(context.read<ApiClient>()).syncPendingOperations();
      if (!mounted) return;
      await _reloadPending();
      messenger.showSnackBar(
        SnackBar(
            content: Text(
                'Sincronizacion completada. Operaciones enviadas: $synced')),
      );
    } catch (_) {
      if (!mounted) return;
      messenger.showSnackBar(
        const SnackBar(
            content: Text('No fue posible completar la sincronizacion.')),
      );
    } finally {
      if (mounted) {
        setState(() => _syncing = false);
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final auth = context.watch<AuthProvider>();
    return Scaffold(
      appBar: AppBar(title: const Text('Ajustes')),
      body: FutureBuilder<int>(
        future: _pendingFuture,
        builder: (context, snapshot) {
          final pending = snapshot.data ?? 0;
          return ListView(
            padding: const EdgeInsets.all(16),
            children: [
              const Text('Conexion al servidor',
                  style: TextStyle(fontSize: 18, fontWeight: FontWeight.bold)),
              const SizedBox(height: 8),
              Card(
                child: ListTile(
                  title: Text('${AppConfig.serverIp}:${AppConfig.serverPort}'),
                  subtitle: Text(
                    auth.hasCapability(UserCapability.manageServerConfig)
                        ? 'Servidor actual configurado'
                        : 'Visible en modo solo lectura',
                  ),
                  trailing:
                      auth.hasCapability(UserCapability.manageServerConfig)
                          ? const Icon(Icons.chevron_right)
                          : null,
                  onTap: auth.hasCapability(UserCapability.manageServerConfig)
                      ? () {
                          Navigator.of(context).push(
                            MaterialPageRoute(
                                builder: (_) => const ServerConfigScreen()),
                          );
                        }
                      : null,
                ),
              ),
              const SizedBox(height: 16),
              const Text('Sesion',
                  style: TextStyle(fontSize: 18, fontWeight: FontWeight.bold)),
              Card(
                child: ListTile(
                  title: Text(auth.session?.displayName ?? 'Sin sesion'),
                  subtitle: Text(auth.roleLabel),
                ),
              ),
              const SizedBox(height: 16),
              const Text('Sincronizacion',
                  style: TextStyle(fontSize: 18, fontWeight: FontWeight.bold)),
              Card(
                child: ListTile(
                  title: Text('$pending registro(s) pendientes'),
                  subtitle: Text(
                    pending == 0
                        ? 'No hay operaciones offline pendientes'
                        : 'Hay operaciones pendientes de sincronizacion',
                  ),
                  trailing: _syncing
                      ? const SizedBox(
                          height: 20,
                          width: 20,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : TextButton(
                          onPressed: pending == 0 ? null : _syncNow,
                          child: const Text('Sincronizar'),
                        ),
                ),
              ),
              const SizedBox(height: 16),
              if (auth.hasCapability(UserCapability.manageUbicaciones)) ...[
                const SizedBox(height: 16),
                const Text('Catalogos',
                    style:
                        TextStyle(fontSize: 18, fontWeight: FontWeight.bold)),
                Card(
                  child: ListTile(
                    title: const Text('Ubicaciones'),
                    subtitle:
                        const Text('Gestionar ubicaciones activas e inactivas'),
                    trailing: const Icon(Icons.chevron_right),
                    onTap: () {
                      Navigator.of(context).push(
                        MaterialPageRoute(
                            builder: (_) => const UbicacionesScreen()),
                      );
                    },
                  ),
                ),
              ],
              const SizedBox(height: 16),
              ElevatedButton(
                onPressed: () async {
                  try {
                    await context
                        .read<PushNotificacionService>()
                        .limpiarToken();
                  } catch (_) {}
                  if (context.mounted) context.read<AuthProvider>().logout();
                },
                child: const Text('Cerrar sesion'),
              ),
            ],
          );
        },
      ),
    );
  }
}
