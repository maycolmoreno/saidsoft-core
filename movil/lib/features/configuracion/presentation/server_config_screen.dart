import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../../core/config/app_config.dart';
import '../../auth/presentation/auth_provider.dart';
import 'server_config_provider.dart';

class ServerConfigScreen extends StatefulWidget {
  const ServerConfigScreen({
    super.key,
    this.showContinue = false,
  });

  final bool showContinue;

  @override
  State<ServerConfigScreen> createState() => _ServerConfigScreenState();
}

class _ServerConfigScreenState extends State<ServerConfigScreen> {
  late final TextEditingController _ipController;
  late final TextEditingController _portController;

  @override
  void initState() {
    super.initState();
    _ipController = TextEditingController(text: AppConfig.serverIp);
    _portController =
        TextEditingController(text: AppConfig.serverPort.toString());
  }

  @override
  void dispose() {
    _ipController.dispose();
    _portController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final provider = context.watch<ServerConfigProvider>();
    return Scaffold(
      appBar: AppBar(title: const Text('Servidor CRESIO')),
      body: SafeArea(
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            const SizedBox(height: 12),
            const Text(
              'Configura la IP y el puerto del servidor interno de la empresa.',
              style: TextStyle(fontSize: 16),
            ),
            const SizedBox(height: 24),
            TextField(
              controller: _ipController,
              keyboardType: TextInputType.number,
              decoration: const InputDecoration(
                labelText: 'IP del servidor',
                hintText: '192.168.1.23',
              ),
            ),
            const SizedBox(height: 16),
            TextField(
              controller: _portController,
              keyboardType: TextInputType.number,
              decoration: const InputDecoration(
                labelText: 'Puerto',
                hintText: '8084',
              ),
            ),
            const SizedBox(height: 20),
            if (provider.message != null)
              Padding(
                padding: const EdgeInsets.only(bottom: 16),
                child: Text(
                  provider.message!,
                  style: TextStyle(
                    color: provider.success
                        ? Colors.green.shade700
                        : Colors.red.shade700,
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ),
            ElevatedButton(
              onPressed: provider.loading ? null : _testConnection,
              child: Text(provider.loading ? 'Probando...' : 'Probar conexion'),
            ),
            const SizedBox(height: 12),
            OutlinedButton(
              onPressed: provider.loading ? null : _saveAndContinue,
              child: const Text('Guardar y continuar'),
            ),
          ],
        ),
      ),
    );
  }

  Future<void> _testConnection() async {
    final provider = context.read<ServerConfigProvider>();
    final port = int.tryParse(_portController.text.trim()) ?? 8082;
    final ok = await provider.testConnection(_ipController.text.trim(), port);
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(ok ? 'Servidor conectado' : 'No se pudo conectar'),
        backgroundColor: ok ? Colors.green : Colors.red,
      ),
    );
  }

  Future<void> _saveAndContinue() async {
    final provider = context.read<ServerConfigProvider>();
    final auth = context.read<AuthProvider>();
    final port = int.tryParse(_portController.text.trim()) ?? 8082;
    await provider.save(_ipController.text.trim(), port);
    if (!mounted) return;
    auth.markServerConfigured();
  }
}
