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
///
/// **Ese orden lo calcula el BACKEND** (`services.ordenar_por_urgencia`) y acá solo se
/// renderiza. Hasta el 26-sep-2026 la regla estaba escrita dos veces —los pesos vivían
/// también acá, en Dart— y el panel ordenaba por fecha: la misma lista en dos órdenes,
/// en dos lenguajes, sin nada que delatara la diferencia. Si hace falta cambiar qué va
/// primero, se cambia allá y las dos superficies se mueven juntas.
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

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        // El filtro vive acá y no en la barra superior: la barra la arma el
        // contenedor (Inicio), que es el que tiene el menu de la sesion.
        Align(
          alignment: Alignment.centerRight,
          child: Padding(
            padding: const EdgeInsets.only(right: 8, top: 4),
            child: TextButton.icon(
              onPressed: () => setState(() => _soloAbiertos = !_soloAbiertos),
              icon: Icon(_soloAbiertos ? Icons.filter_alt : Icons.filter_alt_off, size: 18),
              label: Text(_soloAbiertos ? 'Solo abiertos' : 'Todos'),
            ),
          ),
        ),
        Expanded(
          child: FutureBuilder<List<Mantenimiento>>(
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

          // Sin reordenar: viene ordenado por urgencia desde el servidor.
          final todos = snap.data ?? const <Mantenimiento>[];
          final items = _soloAbiertos
              ? todos.where((m) => m.abierto).toList()
              : todos.toList();

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
        ),
      ],
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
