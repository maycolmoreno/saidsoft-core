import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../comun/tema.dart';
import 'estado_gps.dart';

/// Control del envío de ubicación. El envío en sí vive en [EstadoGps], a nivel de
/// app: esta pantalla solo lo enciende, lo apaga y muestra su estado.
class PantallaGps extends StatefulWidget {
  const PantallaGps({super.key});

  @override
  State<PantallaGps> createState() => _PantallaGpsState();
}

class _PantallaGpsState extends State<PantallaGps> {
  @override
  void initState() {
    super.initState();
    final gps = context.read<EstadoGps>();
    if (!gps.consultado) gps.cargarConsentimiento();
  }

  @override
  Widget build(BuildContext context) {
    final gps = context.watch<EstadoGps>();
    return !gps.consultado
          ? const Center(child: CircularProgressIndicator())
          : ListView(
              padding: const EdgeInsets.all(20),
              children: [
                if (!gps.consentimiento) ...[
                  const Icon(Icons.place_outlined, size: 56, color: Tema.primario),
                  const SizedBox(height: 16),
                  const Text(
                    'Consentimiento de monitoreo',
                    textAlign: TextAlign.center,
                    style: TextStyle(fontSize: 18, fontWeight: FontWeight.w700),
                  ),
                  const SizedBox(height: 16),
                  const Card(
                    child: Padding(
                      padding: EdgeInsets.all(16),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            'Para coordinar el trabajo en campo, la app envia tu '
                            'ubicacion mientras el envio este activo.',
                          ),
                          SizedBox(height: 12),
                          Text('• Se envia cada 30 segundos.'),
                          Text('• Arranca solo al registrar tu llegada a un mantenimiento.'),
                          Text('• Podes detenerlo cuando quieras.'),
                          Text('• Sirve para confirmar tu presencia en la farmacia.'),
                        ],
                      ),
                    ),
                  ),
                  const SizedBox(height: 20),
                  FilledButton.icon(
                    onPressed: () => gps.aceptarConsentimiento(),
                    icon: const Icon(Icons.check),
                    label: const Text('Acepto el monitoreo'),
                  ),
                ] else ...[
                  Card(
                    child: Padding(
                      padding: const EdgeInsets.all(20),
                      child: Column(
                        children: [
                          Icon(
                            gps.enviando ? Icons.gps_fixed : Icons.gps_off,
                            size: 48,
                            color: gps.enviando ? Tema.bien : Colors.grey.shade400,
                          ),
                          const SizedBox(height: 12),
                          Text(
                            gps.enviando ? 'Enviando ubicacion' : 'Envio detenido',
                            style: const TextStyle(
                                fontSize: 16, fontWeight: FontWeight.w700),
                          ),
                          if (gps.ultimo.isNotEmpty) ...[
                            const SizedBox(height: 6),
                            Text(gps.ultimo,
                                style: TextStyle(
                                    fontSize: 12, color: Colors.grey.shade600)),
                          ],
                          if (gps.enviadas > 0) ...[
                            const SizedBox(height: 4),
                            Text('${gps.enviadas} envio(s) en esta sesion',
                                style: TextStyle(
                                    fontSize: 12, color: Colors.grey.shade600)),
                          ],
                        ],
                      ),
                    ),
                  ),
                  const SizedBox(height: 20),
                  FilledButton.icon(
                    onPressed: () => gps.enviando ? gps.detener() : gps.comenzar(),
                    style: gps.enviando
                        ? FilledButton.styleFrom(backgroundColor: Tema.critico)
                        : null,
                    icon: Icon(gps.enviando ? Icons.stop : Icons.play_arrow),
                    label: Text(gps.enviando ? 'Detener envio' : 'Comenzar envio'),
                  ),
                  const SizedBox(height: 16),
                  Text(
                    'El envio sigue activo aunque cambies de pantalla. Se detiene si '
                    'cerras la app.',
                    textAlign: TextAlign.center,
                    style: TextStyle(fontSize: 12, color: Colors.grey.shade600),
                  ),
                ],
                if (gps.error != null) ...[
                  const SizedBox(height: 20),
                  Text(
                    gps.error!,
                    textAlign: TextAlign.center,
                    style: TextStyle(color: Colors.red.shade700, fontSize: 13),
                  ),
                ],
              ],
            );
  }
}
