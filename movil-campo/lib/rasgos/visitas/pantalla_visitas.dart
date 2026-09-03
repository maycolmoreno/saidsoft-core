import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../comun/tema.dart';
import '../../nucleo/red/api.dart';
import '../sesion/estado_sesion.dart';
import '../sesion/sesion.dart';
import 'repo_visitas.dart';
import 'visita.dart';

class PantallaVisitas extends StatefulWidget {
  const PantallaVisitas({super.key});

  @override
  State<PantallaVisitas> createState() => _PantallaVisitasState();
}

class _PantallaVisitasState extends State<PantallaVisitas> {
  late RepoVisitas _repo;
  late Future<List<Visita>> _futuro;
  bool _trabajando = false;

  @override
  void initState() {
    super.initState();
    _repo = RepoVisitas(context.read<Api>());
    _futuro = _repo.listar();
  }

  Future<void> _recargar() async {
    final futuro = _repo.listar();
    setState(() => _futuro = futuro);
    await futuro;
  }

  Future<void> _ejecutar(Future<bool> Function() accion, String exito) async {
    setState(() => _trabajando = true);
    try {
      final pendiente = await accion();
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(pendiente
              ? 'Sin conexion: quedo pendiente y se envia al recuperar senal.'
              : exito),
          backgroundColor: pendiente ? Tema.advertencia : null,
        ),
      );
      if (!pendiente) await _recargar();
    } on ErrorApi catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text(e.mensaje)));
      }
    } finally {
      if (mounted) setState(() => _trabajando = false);
    }
  }

  Future<void> _cerrar(Visita v) async {
    final controlador = TextEditingController();
    final observaciones = await showDialog<String>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Cerrar visita'),
        content: TextField(
          controller: controlador,
          minLines: 3,
          maxLines: 5,
          decoration: const InputDecoration(
            labelText: 'Que encontraste',
            hintText: 'Opcional',
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(),
            child: const Text('Cancelar'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(ctx).pop(controlador.text.trim()),
            child: const Text('Cerrar visita'),
          ),
        ],
      ),
    );
    controlador.dispose();
    if (observaciones == null) return;
    await _ejecutar(() => _repo.cerrar(v.id, observaciones), 'Visita cerrada.');
  }

  @override
  Widget build(BuildContext context) {
    final puedeGestionar =
        context.watch<EstadoSesion>().puede(Permiso.gestionarVisitas);
    return Scaffold(
      appBar: AppBar(title: const Text('Mis visitas')),
      body: Stack(
        children: [
          FutureBuilder<List<Visita>>(
            future: _futuro,
            builder: (context, snap) {
              if (snap.connectionState == ConnectionState.waiting) {
                return const Center(child: CircularProgressIndicator());
              }
              if (snap.hasError) {
                final e = snap.error;
                return EstadoMensaje(
                  icono: e is SinConexion ? Icons.wifi_off : Icons.error_outline,
                  titulo: e is SinConexion ? 'Sin conexion' : 'No se pudo cargar',
                  detalle: e is ErrorApi ? e.mensaje : '$e',
                  onReintentar: _recargar,
                );
              }
              final visitas = snap.data ?? const <Visita>[];
              if (visitas.isEmpty) {
                return RefreshIndicator(
                  onRefresh: _recargar,
                  child: ListView(
                    children: [
                      SizedBox(height: MediaQuery.of(context).size.height * 0.25),
                      const EstadoMensaje(
                        icono: Icons.storefront_outlined,
                        titulo: 'No tenes visitas planificadas',
                        detalle: 'Las visitas se planifican desde el panel.',
                      ),
                    ],
                  ),
                );
              }
              return RefreshIndicator(
                onRefresh: _recargar,
                child: ListView.separated(
                  padding: const EdgeInsets.all(16),
                  itemCount: visitas.length,
                  separatorBuilder: (_, __) => const SizedBox(height: 10),
                  itemBuilder: (_, i) => _TarjetaVisita(
                    visita: visitas[i],
                    habilitado: puedeGestionar && !_trabajando,
                    onIniciar: () => _ejecutar(
                      () => _repo.iniciar(visitas[i].id),
                      'Llegada registrada.',
                    ),
                    onCerrar: () => _cerrar(visitas[i]),
                  ),
                ),
              );
            },
          ),
          if (_trabajando)
            const Positioned.fill(
              child: ColoredBox(
                color: Color(0x22000000),
                child: Center(child: CircularProgressIndicator()),
              ),
            ),
        ],
      ),
    );
  }
}

class _TarjetaVisita extends StatelessWidget {
  const _TarjetaVisita({
    required this.visita,
    required this.habilitado,
    required this.onIniciar,
    required this.onCerrar,
  });

  final Visita visita;
  final bool habilitado;
  final VoidCallback onIniciar;
  final VoidCallback onCerrar;

  @override
  Widget build(BuildContext context) {
    final v = visita;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(
                    v.farmacia == null
                        ? 'Sin farmacia'
                        : '${v.farmacia!.codigo} · ${v.farmacia!.nombre}',
                    style: const TextStyle(
                        fontWeight: FontWeight.w700, fontSize: 15),
                  ),
                ),
                Pastilla(
                  v.etiquetaEstado,
                  color: switch (v.estado) {
                    'realizada' => Tema.bien,
                    'en_curso' => Tema.advertencia,
                    'cancelada' => Tema.neutro,
                    _ => Tema.primario,
                  },
                ),
              ],
            ),
            if (v.farmacia != null && v.farmacia!.direccion.isNotEmpty) ...[
              const SizedBox(height: 4),
              Text(v.farmacia!.direccion,
                  style: TextStyle(fontSize: 12, color: Colors.grey.shade600)),
            ],
            if (v.motivo.isNotEmpty) ...[
              const SizedBox(height: 8),
              Text(v.motivo, style: const TextStyle(fontSize: 13)),
            ],
            const SizedBox(height: 10),
            Wrap(
              spacing: 8,
              runSpacing: 6,
              children: [
                if (v.fechaLegible.isNotEmpty)
                  Pastilla(v.fechaLegible,
                      color: Tema.neutro, icono: Icons.event),
                if (v.atrasada) const Pastilla('Atrasada', color: Tema.critico),
                if (v.realizada)
                  Pastilla(
                    switch (v.presencia) {
                      Presencia.verificada => 'Presencia verificada',
                      Presencia.fueraDeRango =>
                        'Fuera de rango (${v.distanciaMetros?.round()} m)',
                      // "Sin datos" NO acusa: puede no haber habido senal.
                      Presencia.sinDatos => 'Presencia sin datos',
                    },
                    color: switch (v.presencia) {
                      Presencia.verificada => Tema.bien,
                      Presencia.fueraDeRango => Tema.advertencia,
                      Presencia.sinDatos => Tema.neutro,
                    },
                    icono: Icons.place_outlined,
                  ),
              ],
            ),
            if (habilitado && (v.planificada || v.enCurso)) ...[
              const SizedBox(height: 12),
              if (v.planificada)
                OutlinedButton.icon(
                  onPressed: onIniciar,
                  icon: const Icon(Icons.play_arrow),
                  label: const Text('Registrar llegada'),
                )
              else
                FilledButton.icon(
                  onPressed: onCerrar,
                  icon: const Icon(Icons.check),
                  label: const Text('Cerrar visita'),
                ),
            ],
          ],
        ),
      ),
    );
  }
}
