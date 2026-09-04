import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../comun/tema.dart';
import 'estado_sesion.dart';

/// Puerta de entrada cuando la sesión está guardada pero con cerradura biométrica.
///
/// No es la pantalla de ingreso: acá NO se piden usuario ni clave, porque la sesión ya
/// existe. Solo se confirma que quien tiene el teléfono es su dueño.
class PantallaBloqueo extends StatefulWidget {
  const PantallaBloqueo({super.key});

  @override
  State<PantallaBloqueo> createState() => _PantallaBloqueoState();
}

class _PantallaBloqueoState extends State<PantallaBloqueo> {
  @override
  void initState() {
    super.initState();
    // Se pide la huella sola al abrir: obligar a tocar un botón antes agrega un paso
    // a algo que el técnico hace decenas de veces por día.
    WidgetsBinding.instance.addPostFrameCallback((_) => _pedir());
  }

  Future<void> _pedir() async {
    if (!mounted) return;
    await context.read<EstadoSesion>().desbloquear();
  }

  @override
  Widget build(BuildContext context) {
    final estado = context.watch<EstadoSesion>();
    final nombre = estado.sesion?.nombre ?? '';

    return Scaffold(
      body: SafeArea(
        child: Center(
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 32),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                const Icon(Icons.fingerprint, size: 72, color: Tema.primario),
                const SizedBox(height: 20),
                Text(
                  nombre.isEmpty ? 'Sesion bloqueada' : 'Hola, $nombre',
                  style: const TextStyle(fontSize: 20, fontWeight: FontWeight.w700),
                  textAlign: TextAlign.center,
                ),
                const SizedBox(height: 8),
                Text(
                  'Usa tu huella para entrar.',
                  style: TextStyle(fontSize: 14, color: Colors.grey.shade600),
                  textAlign: TextAlign.center,
                ),
                if (estado.error != null) ...[
                  const SizedBox(height: 16),
                  Text(
                    estado.error!,
                    style: const TextStyle(fontSize: 13, color: Tema.critico),
                    textAlign: TextAlign.center,
                  ),
                ],
                const SizedBox(height: 28),
                SizedBox(
                  width: double.infinity,
                  child: FilledButton.icon(
                    onPressed: estado.ocupado ? null : _pedir,
                    icon: const Icon(Icons.fingerprint),
                    label: Text(estado.ocupado ? 'Verificando...' : 'Reintentar'),
                  ),
                ),
                const SizedBox(height: 8),
                // Salida de emergencia: si la biometria dejo de funcionar, se vuelve a
                // usuario y clave en vez de quedar preso fuera de la app.
                TextButton(
                  onPressed: estado.ocupado
                      ? null
                      : () => context.read<EstadoSesion>().olvidarSesionBloqueada(),
                  child: const Text('Entrar con usuario y clave'),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
