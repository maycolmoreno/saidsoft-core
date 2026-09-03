import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../auth/presentation/auth_provider.dart';
import 'gps_provider.dart';

class ConsentimientoGpsScreen extends StatelessWidget {
  const ConsentimientoGpsScreen({super.key});

  @override
  Widget build(BuildContext context) {
    final gps = context.watch<GpsProvider>();
    final auth = context.read<AuthProvider>();

    return Scaffold(
      appBar: AppBar(title: const Text('Monitoreo GPS')),
      body: ListView(
        padding: const EdgeInsets.all(24),
        children: [
          const Icon(Icons.location_on, size: 64, color: Color(0xFF47267F)),
          const SizedBox(height: 16),
          const Text(
            'Consentimiento de monitoreo',
            style: TextStyle(fontSize: 22, fontWeight: FontWeight.bold),
            textAlign: TextAlign.center,
          ),
          const SizedBox(height: 16),
          const Card(
            child: Padding(
              padding: EdgeInsets.all(16),
              child: Text(
                'Para mejorar la gestión de actividades en campo, '
                'la aplicación enviará tu ubicación periódicamente '
                'mientras el rastreo esté activo.\n\n'
                '• Tu ubicación se envía cada 30 segundos.\n'
                '• Puedes detener el envío en cualquier momento.\n'
                '• Los datos se usan exclusivamente para coordinación operativa.',
                style: TextStyle(fontSize: 15),
              ),
            ),
          ),
          const SizedBox(height: 24),
          if (!gps.consentimientoRegistrado) ...[
            if (gps.errorConsentimiento != null)
              Padding(
                padding: const EdgeInsets.only(bottom: 12),
                child: Text(
                  gps.errorConsentimiento!,
                  style: const TextStyle(color: Colors.red),
                  textAlign: TextAlign.center,
                ),
              ),
            FilledButton.icon(
              onPressed: gps.loadingConsentimiento
                  ? null
                  : () async {
                      final userId = auth.userId;
                      if (userId == null) return;
                      await gps.registrarConsentimiento(userId);
                    },
              icon: gps.loadingConsentimiento
                  ? const SizedBox(
                      width: 18,
                      height: 18,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  : const Icon(Icons.check),
              label: const Text('Acepto el monitoreo'),
            ),
          ] else ...[
            const Divider(),
            const SizedBox(height: 12),
            Text(
              gps.trackingActivo
                  ? 'Rastreo activo — enviando ubicación'
                  : 'Rastreo detenido',
              style: TextStyle(
                fontSize: 16,
                fontWeight: FontWeight.w600,
                color: gps.trackingActivo ? Colors.green : Colors.grey,
              ),
              textAlign: TextAlign.center,
            ),
            const SizedBox(height: 12),
            if (gps.errorUbicacion != null)
              Padding(
                padding: const EdgeInsets.only(bottom: 12),
                child: Text(
                  gps.errorUbicacion!,
                  style: const TextStyle(color: Colors.red),
                  textAlign: TextAlign.center,
                ),
              ),
            if (!gps.trackingActivo)
              FilledButton.icon(
                onPressed: () {
                  final userId = auth.userId;
                  if (userId == null) return;
                  gps.iniciarTracking(userId);
                },
                icon: const Icon(Icons.play_arrow),
                label: const Text('Iniciar rastreo'),
              )
            else
              OutlinedButton.icon(
                onPressed: gps.detenerTracking,
                icon: const Icon(Icons.stop),
                label: const Text('Detener rastreo'),
              ),
          ],
        ],
      ),
    );
  }
}
