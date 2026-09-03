import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../../core/network/api_client.dart';
import '../../auth/data/auth_models.dart';
import '../../auth/presentation/auth_provider.dart';
import '../data/mantenimientos_repository.dart';

class MantenimientoDetailScreen extends StatefulWidget {
  const MantenimientoDetailScreen({
    super.key,
    required this.mantenimientoId,
  });

  final int mantenimientoId;

  @override
  State<MantenimientoDetailScreen> createState() =>
      _MantenimientoDetailScreenState();
}

class _MantenimientoDetailScreenState extends State<MantenimientoDetailScreen> {
  final _observacionesController = TextEditingController();
  final _tiempoController = TextEditingController();
  late Future<Map<String, dynamic>> _future;
  bool _closing = false;
  String? _resultadoTecnico;
  String? _estadoGeneral;

  /// Espejo del catálogo ResultadoTecnico del backend. Se mantiene acá y no se pide
  /// por API para que la pantalla de cierre funcione sin conexión, que es
  /// justamente cuando el técnico está en la farmacia. Si el backend agrega un
  /// resultado nuevo, hay que sumarlo también acá.
  static const _resultados = <String, String>{
    'reparado': 'Reparado',
    'sin_falla': 'Sin falla encontrada',
    'sin_intervencion': 'Sin intervencion',
    'parcialmente_reparado': 'Parcialmente reparado',
    'requiere_repuesto': 'Requiere repuesto',
    'escalado_a_proveedor': 'Escalado a proveedor',
    'irreparable': 'Irreparable',
    'requiere_baja': 'Requiere baja',
    'garantia_aplicada': 'Garantia aplicada',
    'garantia_rechazada': 'Garantia rechazada',
    'actualizado': 'Actualizado',
    'instalado': 'Instalado',
  };

  static const _estadosGenerales = <String, String>{
    'operativo': 'Operativo',
    'requiere_revision': 'Requiere revision',
    'no_operativo': 'No operativo',
  };

  @override
  void initState() {
    super.initState();
    _future = _load();
  }

  @override
  void dispose() {
    _observacionesController.dispose();
    _tiempoController.dispose();
    super.dispose();
  }

  Future<Map<String, dynamic>> _load() {
    return MantenimientosRepository(context.read<ApiClient>())
        .obtenerDetalle(widget.mantenimientoId);
  }

  Future<void> _reload() async {
    final future = _load();
    setState(() {
      _future = future;
    });
    await future;
  }

  Future<void> _cerrar() async {
    // El backend exige un resultado técnico del catálogo: decide si el equipo
    // vuelve a bodega y si se recomienda darlo de baja. No se puede inferir del
    // texto libre, así que se pregunta.
    if (_resultadoTecnico == null) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text('Elige el resultado tecnico para cerrar.'),
        ),
      );
      return;
    }

    setState(() => _closing = true);
    try {
      final queuedOffline =
          await MantenimientosRepository(context.read<ApiClient>())
              .cerrarConFallback(
        mantenimientoId: widget.mantenimientoId,
        resultadoTecnico: _resultadoTecnico!,
        tiempoRealMinutos: int.tryParse(_tiempoController.text.trim()),
        estadoGeneral: _estadoGeneral ?? '',
      );
      _observacionesController.clear();
      _tiempoController.clear();
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            queuedOffline
                ? 'Sin conexion. El cierre quedo pendiente de sincronizacion.'
                : 'Mantenimiento cerrado correctamente.',
          ),
        ),
      );
      if (!queuedOffline) {
        await _reload();
      }
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
            content: Text(
                'Error al cerrar: ${e.toString().replaceAll('Exception: ', '')}')),
      );
    } finally {
      if (mounted) {
        setState(() => _closing = false);
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final canClose = context.watch<AuthProvider>().hasCapability(
          UserCapability.closeMantenimiento,
        );
    return Scaffold(
      appBar: AppBar(title: const Text('Detalle del mantenimiento')),
      body: FutureBuilder<Map<String, dynamic>>(
        future: _future,
        builder: (context, snapshot) {
          if (snapshot.connectionState == ConnectionState.waiting) {
            return const Center(child: CircularProgressIndicator());
          }
          if (snapshot.hasError) {
            return _RetryState(
              message: 'No fue posible cargar el mantenimiento.',
              onRetry: _reload,
            );
          }

          final item = snapshot.data ?? const {};
          final cerrado = _text(item['estado_interno']) == 'cerrado';
          final equipos = (item['equipos'] as List?) ?? const [];
          final equipo = equipos.isNotEmpty
              ? Map<String, dynamic>.from(equipos.first as Map)
              : const <String, dynamic>{};
          final farmacia = item['farmacia'] == null
              ? const <String, dynamic>{}
              : Map<String, dynamic>.from(item['farmacia'] as Map);

          return RefreshIndicator(
            onRefresh: _reload,
            child: ListView(
              padding: const EdgeInsets.all(16),
              physics: const AlwaysScrollableScrollPhysics(),
              children: [
                Card(
                  child: Padding(
                    padding: const EdgeInsets.all(16),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          _text(equipo['codigo'], fallback: 'Sin codigo'),
                          style: Theme.of(context).textTheme.titleMedium,
                        ),
                        const SizedBox(height: 12),
                        _line('Equipo', equipo['modelo']),
                        _line('Serie', equipo['numero_serie']),
                        _line('Prioridad', item['prioridad']),
                        // El SLA es lo que le dice al tecnico si esto corre o no.
                        _line('SLA', item['estado_sla']),
                        _line('Limite de resolucion', item['limite_resolucion']),
                        _line('Farmacia', farmacia['nombre']),
                        _line('Direccion', farmacia['direccion']),
                        _line('Cliente', item['cliente']),
                        _line('Estado', item['estado_interno']),
                        _line('Programado', item['fecha_programada']),
                        _line('Descripcion', item['descripcion']),
                        _line('Resultado', item['resultado_tecnico']),
                      ],
                    ),
                  ),
                ),
                const SizedBox(height: 12),
                if (!cerrado && canClose) ...[
                  DropdownButtonFormField<String>(
                    initialValue: _resultadoTecnico,
                    decoration: const InputDecoration(
                      labelText: 'Resultado tecnico *',
                      helperText:
                          'Decide si el equipo vuelve a bodega o se recomienda la baja.',
                    ),
                    items: [
                      for (final entrada in _resultados.entries)
                        DropdownMenuItem(
                          value: entrada.key,
                          child: Text(entrada.value),
                        ),
                    ],
                    onChanged: _closing
                        ? null
                        : (valor) => setState(() => _resultadoTecnico = valor),
                  ),
                  const SizedBox(height: 12),
                  DropdownButtonFormField<String>(
                    initialValue: _estadoGeneral,
                    decoration: const InputDecoration(
                      labelText: 'Estado del equipo al cierre',
                      helperText: 'Opcional.',
                    ),
                    items: [
                      for (final entrada in _estadosGenerales.entries)
                        DropdownMenuItem(
                          value: entrada.key,
                          child: Text(entrada.value),
                        ),
                    ],
                    onChanged: _closing
                        ? null
                        : (valor) => setState(() => _estadoGeneral = valor),
                  ),
                  const SizedBox(height: 12),
                  TextField(
                    controller: _tiempoController,
                    keyboardType: TextInputType.number,
                    decoration: const InputDecoration(
                      labelText: 'Tiempo real de intervencion (minutos)',
                      helperText: 'Opcional.',
                    ),
                  ),
                  const SizedBox(height: 12),
                  TextField(
                    controller: _observacionesController,
                    minLines: 3,
                    maxLines: 5,
                    decoration: const InputDecoration(
                      labelText: 'Observaciones de cierre',
                      hintText: 'Describe el trabajo realizado por el tecnico',
                    ),
                  ),
                  const SizedBox(height: 12),
                  FilledButton.icon(
                    onPressed: _closing ? null : _cerrar,
                    icon: _closing
                        ? const SizedBox(
                            height: 16,
                            width: 16,
                            child: CircularProgressIndicator(strokeWidth: 2),
                          )
                        : const Icon(Icons.check_circle_outline),
                    label:
                        Text(_closing ? 'Cerrando...' : 'Cerrar mantenimiento'),
                  ),
                ] else if (cerrado)
                  const Card(
                    child: Padding(
                      padding: EdgeInsets.all(16),
                      child: Text('Este mantenimiento ya fue cerrado.'),
                    ),
                  )
                else
                  const Card(
                    child: Padding(
                      padding: EdgeInsets.all(16),
                      child: Text(
                          'Tu rol no puede cerrar mantenimientos desde la app.'),
                    ),
                  ),
              ],
            ),
          );
        },
      ),
    );
  }
}

class _RetryState extends StatelessWidget {
  const _RetryState({
    required this.message,
    required this.onRetry,
  });

  final String message;
  final Future<void> Function() onRetry;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Text(message),
          const SizedBox(height: 12),
          OutlinedButton(onPressed: onRetry, child: const Text('Reintentar')),
        ],
      ),
    );
  }
}

Widget _line(String label, dynamic value) {
  final text = _text(value, fallback: '');
  if (text.isEmpty) {
    return const SizedBox.shrink();
  }
  return Padding(
    padding: const EdgeInsets.only(bottom: 8),
    child: RichText(
      text: TextSpan(
        style: const TextStyle(color: Colors.black87),
        children: [
          TextSpan(
            text: '$label: ',
            style: const TextStyle(fontWeight: FontWeight.w700),
          ),
          TextSpan(text: text),
        ],
      ),
    ),
  );
}

String _text(dynamic value, {String fallback = '-'}) {
  final text = value?.toString().trim() ?? '';
  return text.isEmpty ? fallback : text;
}
