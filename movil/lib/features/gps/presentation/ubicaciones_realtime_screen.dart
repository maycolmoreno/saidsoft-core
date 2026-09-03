import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../data/gps_models.dart';
import 'gps_provider.dart';

class UbicacionesRealtimeScreen extends StatefulWidget {
  const UbicacionesRealtimeScreen({super.key});

  @override
  State<UbicacionesRealtimeScreen> createState() =>
      _UbicacionesRealtimeScreenState();
}

class _UbicacionesRealtimeScreenState extends State<UbicacionesRealtimeScreen> {
  late final GpsProvider _gps;

  @override
  void initState() {
    super.initState();
    _gps = context.read<GpsProvider>();
    _gps.iniciarRefrescoAutomatico();
  }

  @override
  void dispose() {
    _gps.detenerRefrescoAutomatico();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final gps = context.watch<GpsProvider>();

    return Scaffold(
      appBar: AppBar(
        title: const Text('Ubicaciones en tiempo real'),
        actions: [
          IconButton(
            icon: const Icon(Icons.refresh),
            onPressed: gps.cargarUbicacionesTiempoReal,
          ),
        ],
      ),
      body: _buildBody(gps),
    );
  }

  Widget _buildBody(GpsProvider gps) {
    if (gps.loadingUbicaciones && gps.ubicacionesActivas.isEmpty) {
      return const Center(child: CircularProgressIndicator());
    }

    if (gps.errorUbicaciones != null && gps.ubicacionesActivas.isEmpty) {
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text('Error: ${gps.errorUbicaciones}',
                  textAlign: TextAlign.center),
              const SizedBox(height: 12),
              FilledButton(
                onPressed: gps.cargarUbicacionesTiempoReal,
                child: const Text('Reintentar'),
              ),
            ],
          ),
        ),
      );
    }

    if (gps.ubicacionesActivas.isEmpty) {
      return const Center(
        child: Padding(
          padding: EdgeInsets.all(24),
          child: Text(
            'No hay técnicos con ubicación activa en las últimas 2 horas.',
            textAlign: TextAlign.center,
          ),
        ),
      );
    }

    return RefreshIndicator(
      onRefresh: gps.cargarUbicacionesTiempoReal,
      child: ListView.builder(
        physics: const AlwaysScrollableScrollPhysics(),
        itemCount: gps.ubicacionesActivas.length,
        itemBuilder: (context, index) {
          final u = gps.ubicacionesActivas[index];
          return _UbicacionTile(ubicacion: u);
        },
      ),
    );
  }
}

class _UbicacionTile extends StatelessWidget {
  const _UbicacionTile({required this.ubicacion});

  final UbicacionActivaResponse ubicacion;

  @override
  Widget build(BuildContext context) {
    final tiempoTexto = ubicacion.minutosAtras < 1
        ? 'Ahora'
        : 'Hace ${ubicacion.minutosAtras} min';

    return Card(
      margin: const EdgeInsets.symmetric(horizontal: 16, vertical: 6),
      child: ListTile(
        leading: CircleAvatar(
          backgroundColor: ubicacion.minutosAtras < 5
              ? Colors.green
              : ubicacion.minutosAtras < 30
                  ? Colors.orange
                  : Colors.grey,
          child: const Icon(Icons.person_pin_circle, color: Colors.white),
        ),
        title: Text(ubicacion.nombre),
        subtitle: Text(
          '${ubicacion.departamento ?? 'Sin departamento'}\n'
          'Lat: ${ubicacion.latitud.toStringAsFixed(5)}, '
          'Lng: ${ubicacion.longitud.toStringAsFixed(5)}'
          '${ubicacion.precisionMetros != null ? ' (±${ubicacion.precisionMetros!.toStringAsFixed(0)}m)' : ''}',
        ),
        isThreeLine: true,
        trailing: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Icon(
              Icons.circle,
              size: 12,
              color: ubicacion.minutosAtras < 5 ? Colors.green : Colors.grey,
            ),
            const SizedBox(height: 4),
            Text(
              tiempoTexto,
              style: const TextStyle(fontSize: 12),
            ),
          ],
        ),
      ),
    );
  }
}
