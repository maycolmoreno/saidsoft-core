import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import 'package:provider/provider.dart';

import '../../comun/tema.dart';
import '../../nucleo/red/api.dart';
import '../mantenimientos/pantalla_detalle.dart';
import 'repo_avisos.dart';

class PantallaAvisos extends StatefulWidget {
  const PantallaAvisos({super.key});

  @override
  State<PantallaAvisos> createState() => _PantallaAvisosState();
}

class _PantallaAvisosState extends State<PantallaAvisos> {
  late RepoAvisos _repo;
  late Future<List<Aviso>> _futuro;

  @override
  void initState() {
    super.initState();
    _repo = RepoAvisos(context.read<Api>());
    _futuro = _repo.listar();
  }

  Future<void> _recargar() async {
    final futuro = _repo.listar();
    setState(() => _futuro = futuro);
    await futuro;
  }

  /// Abre el mantenimiento del aviso y lo marca leído.
  ///
  /// Se marca aunque el mantenimiento ya no exista o falle la navegación: el técnico
  /// lo vio, y dejarlo sin leer haría que el contador nunca baje.
  Future<void> _abrir(Aviso aviso) async {
    try {
      if (!aviso.leida) await _repo.marcarLeido(aviso.id);
    } on ErrorApi {
      // No es motivo para no abrirlo.
    }
    if (!mounted) return;
    if (aviso.mantenimientoId != null) {
      await Navigator.of(context).push(
        MaterialPageRoute(
          builder: (_) => PantallaDetalle(id: aviso.mantenimientoId!),
        ),
      );
    }
    await _recargar();
  }

  @override
  Widget build(BuildContext context) {
    return FutureBuilder<List<Aviso>>(
      future: _futuro,
      builder: (context, snap) {
        if (snap.connectionState == ConnectionState.waiting) {
          return const Center(child: CircularProgressIndicator());
        }
        if (snap.hasError) {
          final e = snap.error;
          return EstadoMensaje(
            icono: e is SinConexion ? Icons.wifi_off : Icons.error_outline,
            titulo: e is SinConexion ? 'Sin conexion' : 'No se pudieron cargar los avisos',
            detalle: e is ErrorApi ? e.mensaje : '$e',
            onReintentar: _recargar,
          );
        }

        final avisos = snap.data ?? const <Aviso>[];
        if (avisos.isEmpty) {
          return RefreshIndicator(
            onRefresh: _recargar,
            child: ListView(
              children: [
                SizedBox(height: MediaQuery.of(context).size.height * 0.25),
                const EstadoMensaje(
                  icono: Icons.notifications_none,
                  titulo: 'No tenes avisos',
                  detalle: 'Aca aparecen las asignaciones nuevas y los vencimientos.',
                ),
              ],
            ),
          );
        }

        return RefreshIndicator(
          onRefresh: _recargar,
          child: ListView.separated(
            padding: const EdgeInsets.all(12),
            itemCount: avisos.length,
            separatorBuilder: (_, __) => const SizedBox(height: 8),
            itemBuilder: (_, i) {
              final a = avisos[i];
              return Card(
                child: ListTile(
                  leading: Icon(
                    a.leida ? Icons.notifications_none : Icons.notifications_active,
                    color: a.leida ? Colors.grey.shade400 : Tema.primario,
                  ),
                  title: Text(
                    a.mensaje,
                    style: TextStyle(
                      fontSize: 14,
                      fontWeight: a.leida ? FontWeight.normal : FontWeight.w600,
                    ),
                  ),
                  subtitle: a.creadoEn == null
                      ? null
                      : Text(
                          DateFormat('dd/MM/yyyy HH:mm').format(a.creadoEn!.toLocal()),
                          style: TextStyle(fontSize: 12, color: Colors.grey.shade600),
                        ),
                  trailing: a.mantenimientoId == null
                      ? null
                      : const Icon(Icons.chevron_right),
                  onTap: () => _abrir(a),
                ),
              );
            },
          ),
        );
      },
    );
  }
}
