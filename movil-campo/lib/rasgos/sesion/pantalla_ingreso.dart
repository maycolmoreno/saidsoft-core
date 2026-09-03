import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../nucleo/config.dart';
import 'estado_sesion.dart';
import 'pantalla_servidor.dart';

class PantallaIngreso extends StatefulWidget {
  const PantallaIngreso({super.key});

  @override
  State<PantallaIngreso> createState() => _PantallaIngresoState();
}

class _PantallaIngresoState extends State<PantallaIngreso> {
  final _usuario = TextEditingController();
  final _clave = TextEditingController();
  bool _verClave = false;

  @override
  void dispose() {
    _usuario.dispose();
    _clave.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final estado = context.watch<EstadoSesion>();
    return Scaffold(
      body: SafeArea(
        child: ListView(
          padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 40),
          children: [
            const SizedBox(height: 20),
            Icon(Icons.build_circle, size: 64, color: Theme.of(context).colorScheme.primary),
            const SizedBox(height: 16),
            const Text(
              'SAIDSOFT Campo',
              textAlign: TextAlign.center,
              style: TextStyle(fontSize: 24, fontWeight: FontWeight.w700),
            ),
            const SizedBox(height: 6),
            Text(
              'Tecnicos en campo',
              textAlign: TextAlign.center,
              style: TextStyle(color: Colors.grey.shade600),
            ),
            const SizedBox(height: 40),
            TextField(
              controller: _usuario,
              decoration: const InputDecoration(
                labelText: 'Usuario',
                prefixIcon: Icon(Icons.person_outline),
              ),
              autocorrect: false,
              enableSuggestions: false,
              textInputAction: TextInputAction.next,
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _clave,
              obscureText: !_verClave,
              decoration: InputDecoration(
                labelText: 'Clave',
                prefixIcon: const Icon(Icons.lock_outline),
                suffixIcon: IconButton(
                  icon: Icon(_verClave ? Icons.visibility_off : Icons.visibility),
                  onPressed: () => setState(() => _verClave = !_verClave),
                ),
              ),
              onSubmitted: (_) => _entrar(),
            ),
            if (estado.error != null) ...[
              const SizedBox(height: 16),
              Text(
                estado.error!,
                style: TextStyle(color: Colors.red.shade700, fontSize: 13),
              ),
            ],
            const SizedBox(height: 24),
            FilledButton(
              onPressed: estado.ocupado ? null : _entrar,
              child: estado.ocupado
                  ? const SizedBox(
                      height: 20, width: 20, child: CircularProgressIndicator(strokeWidth: 2))
                  : const Text('Entrar'),
            ),
            const SizedBox(height: 20),
            // La configuración del servidor queda accesible desde el login: si el
            // técnico no puede entrar por apuntar al servidor equivocado, tiene que
            // poder corregirlo sin reinstalar.
            TextButton.icon(
              onPressed: () => Navigator.of(context).push(
                MaterialPageRoute(builder: (_) => const PantallaServidor()),
              ),
              icon: const Icon(Icons.dns_outlined, size: 18),
              label: Text('Servidor: ${Config.ip}:${Config.puerto}'),
            ),
          ],
        ),
      ),
    );
  }

  void _entrar() {
    context.read<EstadoSesion>().iniciarSesion(_usuario.text, _clave.text);
  }
}
