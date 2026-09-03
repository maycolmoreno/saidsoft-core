import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../comun/tema.dart';
import '../../nucleo/red/api.dart';
import 'mantenimiento.dart';
import 'pantalla_detalle.dart';
import 'repo_mantenimientos.dart';

/// Trabajo del técnico, ordenado por urgencia real.
///
/// El orden lo decide el SLA, no la fecha: un correctivo crítico creado hace 10
/// minutos va ANTES que un preventivo agendado la semana pasada. Ordenar por fecha
/// —lo obvio— enterraría justamente lo que no puede esperar.
class PantallaMantenimientos extends StatefulWidget {
  const PantallaMantenimientos({super.key});

  @override
  State<PantallaMantenimientos> createState() => _PantallaMantenimientosState();
}

class _PantallaMantenimientosState extends State<PantallaMantenimientos> {
  late Future<List<Mantenimiento>> _futuro;
  bool _soloAbiertos = true;

  @override
  void initState() {
    super.initState();
    _futuro = _cargar();
  }

  Future<List<Mantenimiento>> _cargar() =>
      RepoMantenimientos(context.read<Api>()).listar();

  Future<void> _recargar() async {
    final futuro = _cargar();
    setState(() => _futuro = futuro);
    await futuro;
  }

  static const _pesoSla = {
    EstadoSla.incumplido: 0,
    EstadoSla.porVencer: 1,
    EstadoSla.enPlazo: 2,
    EstadoSla.sinSla: 3,
    EstadoSla.cumplido: 4,
  };
  static const _pesoPrioridad = {'critica': 0, 'alta': 1, 'normal': 2, 'baja': 3};

  List<Mantenimiento> _ordenar(List<Mantenimiento> items) {
    final lista = [...items];
    lista.sort((a, b) {
      // Los abiertos primero: un cerrado nunca es lo próximo a hacer.
      final abiertoA = a.abierto ? 0 : 1;
      final abiertoB = b.abierto ? 0 : 1;
      if (abiertoA != abiertoB) return abiertoA.compareTo(abiertoB);

      final slaA = _pesoSla[a.estadoSla] ?? 9;
      final slaB = _pesoSla[b.estadoSla] ?? 9;
      if (slaA != slaB) return slaA.compareTo(slaB);

      final prioA = _pesoPrioridad[a.prioridad] ?? 9;
      final prioB = _pesoPrioridad[b.prioridad] ?? 9;
      if (prioA != prioB) return prioA.compareTo(prioB);

      final fa = a.limiteResolucion ?? a.fechaProgramada;
      final fb = b.limiteResolucion ?? b.fechaProgramada;
      if (fa == null || fb == null) return 0;
      return fa.compareTo(fb);
    });
    return lista;
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Mis mantenimientos'),
        actions: [
          IconButton(
            tooltip: _soloAbiertos ? 'Ver todos' : 'Ver solo abiertos',
            icon: Icon(_soloAbiertos ? Icons.filter_alt : Icons.filter_alt_off),
            onPressed: () => setState(() => _soloAbiertos = !_soloAbiertos),
          ),
        ],
      ),
      body: FutureBuilder<List<Mantenimiento>>(
        future: _futuro,
        builder: (context, snap) {
          if (snap.connectionState == ConnectionState.waiting) {
            return const Center(child: CircularProgressIndicator());
          }
          if (snap.hasError) {
            final error = snap.error;
            return EstadoMensaje(
              icono: error is SinConexion ? Icons.wifi_off : Icons.error_outline,
              titulo: error is SinConexion
                  ? 'Sin conexion'
                  : 'No se pudo cargar tu trabajo',
              detalle: error is ErrorApi ? error.mensaje : '$error',
              onReintentar: _recargar,
            );
          }

          final todos = _ordenar(snap.data ?? const []);
          final items = _soloAbiertos
              ? todos.where((m) => m.abierto).toList()
              : todos;

          if (items.isEmpty) {
            return RefreshIndicator(
              onRefresh: _recargar,
              child: ListView(
                children: [
                  SizedBox(height: MediaQuery.of(context).size.height * 0.25),
                  EstadoMensaje(
                    icono: Icons.check_circle_outline,
                    titulo: _soloAbiertos
                        ? 'No tenes mantenimientos abiertos'
                        : 'No tenes mantenimientos asignados',
                    detalle: _soloAbiertos
                        ? 'Desliza para actualizar.'
                        : 'Cuando te asignen uno va a aparecer aca.',
                  ),
                ],
              ),
            );
          }

          return RefreshIndicator(
            onRefresh: _recargar,
            child: ListView.separated(
              padding: const EdgeInsets.all(16),
              itemCount: items.length,
              separatorBuilder: (_, __) => const SizedBox(height: 10),
              itemBuilder: (_, i) => _Tarjeta(
                mantenimiento: items[i],
                onAbrir: () async {
                  await Navigator.of(context).push(
                    MaterialPageRoute(
                      builder: (_) => PantallaDetalle(id: items[i].id),
                    ),
                  );
                  await _recargar();
                },
              ),
            ),
          );
        },
      ),
    );
  }
}

class _Tarjeta extends StatelessWidget {
  const _Tarjeta({required this.mantenimiento, required this.onAbrir});

  final Mantenimiento mantenimiento;
  final VoidCallback onAbrir;

  Color get _colorSla => switch (mantenimiento.estadoSla) {
        EstadoSla.incumplido => Tema.critico,
        EstadoSla.porVencer => Tema.advertencia,
        EstadoSla.enPlazo || EstadoSla.cumplido => Tema.bien,
        EstadoSla.sinSla => Tema.neutro,
      };

  Color get _colorPrioridad => switch (mantenimiento.prioridad) {
        'critica' => Tema.critico,
        'alta' => Tema.advertencia,
        'baja' => Tema.neutro,
        _ => Tema.primario,
      };

  @override
  Widget build(BuildContext context) {
    final m = mantenimiento;
    final equipo = m.equipoPrincipal;
    return Card(
      child: InkWell(
        onTap: onAbrir,
        borderRadius: BorderRadius.circular(12),
        child: Padding(
          padding: const EdgeInsets.all(14),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Expanded(
                    child: Text(
                      equipo?.codigo ?? 'Sin equipo',
                      style: const TextStyle(
                          fontWeight: FontWeight.w700, fontSize: 16),
                    ),
                  ),
                  Pastilla(m.etiquetaPrioridad, color: _colorPrioridad),
                ],
              ),
              if (m.descripcion.isNotEmpty) ...[
                const SizedBox(height: 4),
                Text(
                  m.descripcion,
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(fontSize: 13, color: Colors.grey.shade700),
                ),
              ],
              const SizedBox(height: 10),
              if (m.farmacia != null)
                Row(
                  children: [
                    Icon(Icons.storefront, size: 15, color: Colors.grey.shade600),
                    const SizedBox(width: 6),
                    Expanded(
                      child: Text(
                        '${m.farmacia!.codigo} · ${m.farmacia!.nombre}',
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(fontSize: 12, color: Colors.grey.shade700),
                      ),
                    ),
                  ],
                ),
              const SizedBox(height: 10),
              Wrap(
                spacing: 8,
                runSpacing: 6,
                children: [
                  Pastilla(m.etiquetaEstado, color: Tema.primario),
                  if (m.estadoSla != EstadoSla.sinSla)
                    Pastilla(
                      m.restanteSla.isEmpty ? 'SLA' : m.restanteSla,
                      color: _colorSla,
                      icono: m.estadoSla == EstadoSla.incumplido
                          ? Icons.warning_amber_rounded
                          : Icons.schedule,
                    ),
                ],
              ),
            ],
          ),
        ),
      ),
    );
  }
}
