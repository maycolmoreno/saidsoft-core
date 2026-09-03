import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../comun/tema.dart';
import '../../nucleo/catalogos.dart';
import '../../nucleo/red/api.dart';
import 'mantenimiento.dart';

/// Abrir un mantenimiento desde el campo.
///
/// El técnico está parado frente al equipo, así que el formulario arranca por
/// BUSCARLO —por código o serie, que es lo que puede leer de la etiqueta— y no por
/// elegir un custodio de una lista, como hace el panel.
class PantallaNuevoMantenimiento extends StatefulWidget {
  const PantallaNuevoMantenimiento({super.key});

  @override
  State<PantallaNuevoMantenimiento> createState() =>
      _PantallaNuevoMantenimientoState();
}

class _PantallaNuevoMantenimientoState extends State<PantallaNuevoMantenimiento> {
  late Api _api;
  late Future<Catalogos> _catalogos;

  final _busqueda = TextEditingController();
  final _descripcion = TextEditingController();

  List<Equipo> _resultados = const [];
  Equipo? _elegido;
  bool _buscando = false;
  bool _guardando = false;
  String? _prioridad = 'normal';
  String? _filtroFarmacia;
  String? _filtroCliente;
  String? _estadoGeneral;
  String? _tipo;
  String? _error;

  @override
  void initState() {
    super.initState();
    _api = context.read<Api>();
    _catalogos = context.read<RepoCatalogos>().obtener();
  }

  @override
  void dispose() {
    _busqueda.dispose();
    _descripcion.dispose();
    super.dispose();
  }

  /// Busca por texto libre y/o por los filtros. Con la farmacia elegida y sin texto
  /// lista TODO lo que hay ahi: el tecnico que llega a un local quiere ver el
  /// inventario del sitio, no adivinar el codigo de cada equipo.
  Future<void> _buscar() async {
    final termino = _busqueda.text.trim();
    if (termino.isEmpty && _filtroFarmacia == null && _filtroCliente == null) {
      setState(() => _error = 'Escribi algo para buscar, o elegi una farmacia.');
      return;
    }
    setState(() {
      _buscando = true;
      _error = null;
    });
    try {
      final partes = <String>[
        if (termino.isNotEmpty) 'buscar=${Uri.encodeQueryComponent(termino)}',
        if (_filtroFarmacia != null) 'farmacia=$_filtroFarmacia',
        if (_filtroCliente != null) 'cliente=$_filtroCliente',
      ];
      final datos = await _api.obtener('/equipos/?${partes.join('&')}') as List;
      setState(() {
        _resultados = datos
            .map((e) => Equipo.desdeJson(Map<String, dynamic>.from(e as Map)))
            .toList();
        if (_resultados.isEmpty) {
          _error = 'No se encontro ningun equipo con esos criterios.';
        }
      });
    } on ErrorApi catch (e) {
      setState(() => _error = e.mensaje);
    } finally {
      if (mounted) setState(() => _buscando = false);
    }
  }

  Future<void> _guardar() async {
    if (_elegido == null || _estadoGeneral == null ||
        _descripcion.text.trim().isEmpty) {
      setState(() => _error = 'Elegi el equipo, su estado y describi el problema.');
      return;
    }
    setState(() {
      _guardando = true;
      _error = null;
    });
    try {
      // Se crea directo contra la API y no por el repositorio con cola offline: sin
      // conexion no hay id de mantenimiento, y el tecnico no podria trabajar sobre
      // algo que todavia no existe. Es mejor decirlo que fingir que se creo.
      await _api.publicar('/mantenimientos/', {
        'equipos': [_elegido!.id],
        'descripcion': _descripcion.text.trim(),
        'estado_general': _estadoGeneral,
        'prioridad': _prioridad,
        if (_tipo != null) 'tipo_mantenimiento': int.tryParse(_tipo!),
      });
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Mantenimiento creado y asignado a vos.')),
      );
      Navigator.of(context).pop(true);
    } on ErrorApi catch (e) {
      setState(() => _error = e.mensaje);
    } finally {
      if (mounted) setState(() => _guardando = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Nuevo mantenimiento')),
      body: FutureBuilder<Catalogos>(
        future: _catalogos,
        builder: (context, snap) {
          if (snap.connectionState == ConnectionState.waiting) {
            return const Center(child: CircularProgressIndicator());
          }
          if (snap.hasError) {
            final e = snap.error;
            return EstadoMensaje(
              icono: e is SinConexion ? Icons.wifi_off : Icons.error_outline,
              titulo: e is SinConexion ? 'Sin conexion' : 'No se pudo cargar el formulario',
              detalle: e is ErrorApi ? e.mensaje : '$e',
              onReintentar: () => setState(
                () => _catalogos = context.read<RepoCatalogos>().obtener(),
              ),
            );
          }
          final catalogos = snap.data!;
          return ListView(
            padding: const EdgeInsets.all(16),
            children: [
              const Text('1. Busca el equipo',
                  style: TextStyle(fontWeight: FontWeight.w700)),
              const SizedBox(height: 8),
              Row(
                children: [
                  Expanded(
                    child: TextField(
                      controller: _busqueda,
                      decoration: const InputDecoration(
                        labelText: 'Codigo, serie, farmacia o persona',
                        prefixIcon: Icon(Icons.search),
                      ),
                      onSubmitted: (_) => _buscar(),
                      textInputAction: TextInputAction.search,
                    ),
                  ),
                  const SizedBox(width: 8),
                  SizedBox(
                    height: 52,
                    child: FilledButton(
                      onPressed: _buscando ? null : _buscar,
                      child: _buscando
                          ? const SizedBox(
                              height: 18, width: 18,
                              child: CircularProgressIndicator(strokeWidth: 2))
                          : const Text('Buscar'),
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 12),
              // Filtros para RECORRER, no para adivinar: el tecnico llega a una
              // farmacia y quiere ver todo lo que hay ahi.
              DropdownButtonFormField<String>(
                initialValue: _filtroFarmacia,
                isExpanded: true,
                decoration: const InputDecoration(
                  labelText: 'Filtrar por farmacia',
                  isDense: true,
                ),
                items: [
                  const DropdownMenuItem(value: null, child: Text('Todas')),
                  for (final o in catalogos.farmacias)
                    DropdownMenuItem(value: o.valor, child: Text(o.etiqueta)),
                ],
                onChanged: (v) {
                  setState(() => _filtroFarmacia = v);
                  _buscar();
                },
              ),
              const SizedBox(height: 8),
              DropdownButtonFormField<String>(
                initialValue: _filtroCliente,
                isExpanded: true,
                decoration: const InputDecoration(
                  labelText: 'Filtrar por persona',
                  isDense: true,
                ),
                items: [
                  const DropdownMenuItem(value: null, child: Text('Todas')),
                  for (final o in catalogos.colaboradores)
                    DropdownMenuItem(value: o.valor, child: Text(o.etiqueta)),
                ],
                onChanged: (v) {
                  setState(() => _filtroCliente = v);
                  _buscar();
                },
              ),
              if (_elegido != null) ...[
                const SizedBox(height: 12),
                Card(
                  child: ListTile(
                    leading: const Icon(Icons.check_circle, color: Tema.bien),
                    title: Text(_elegido!.codigo,
                        style: const TextStyle(fontWeight: FontWeight.w700)),
                    subtitle: Text(_elegido!.detalle),
                    trailing: TextButton(
                      onPressed: () => setState(() => _elegido = null),
                      child: const Text('Cambiar'),
                    ),
                  ),
                ),
              ] else
                for (final equipo in _resultados)
                  Card(
                    margin: const EdgeInsets.only(top: 8),
                    child: ListTile(
                      title: Text(equipo.codigo),
                      subtitle: Text(equipo.detalle),
                      onTap: () => setState(() {
                        _elegido = equipo;
                        _resultados = const [];
                      }),
                    ),
                  ),

              const SizedBox(height: 24),
              const Text('2. Describi el problema',
                  style: TextStyle(fontWeight: FontWeight.w700)),
              const SizedBox(height: 8),
              TextField(
                controller: _descripcion,
                minLines: 3,
                maxLines: 5,
                decoration: const InputDecoration(
                  hintText: 'Que le pasa al equipo',
                ),
              ),
              const SizedBox(height: 16),
              DropdownButtonFormField<String>(
                initialValue: _estadoGeneral,
                isExpanded: true,
                decoration: const InputDecoration(labelText: 'Estado del equipo *'),
                items: [
                  for (final o in catalogos.estadosGenerales)
                    DropdownMenuItem(value: o.valor, child: Text(o.etiqueta)),
                ],
                onChanged: (v) => setState(() => _estadoGeneral = v),
              ),
              const SizedBox(height: 12),
              DropdownButtonFormField<String>(
                initialValue: _prioridad,
                isExpanded: true,
                decoration: const InputDecoration(
                  labelText: 'Prioridad',
                  helperText: 'Define en cuanto tiempo hay que resolverlo.',
                ),
                items: [
                  for (final o in catalogos.prioridades)
                    DropdownMenuItem(value: o.valor, child: Text(o.etiqueta)),
                ],
                onChanged: (v) => setState(() => _prioridad = v),
              ),
              const SizedBox(height: 12),
              DropdownButtonFormField<String>(
                initialValue: _tipo,
                isExpanded: true,
                decoration: const InputDecoration(labelText: 'Tipo (opcional)'),
                items: [
                  for (final o in catalogos.tiposMantenimiento)
                    DropdownMenuItem(value: o.valor, child: Text(o.etiqueta)),
                ],
                onChanged: (v) => setState(() => _tipo = v),
              ),

              if (_error != null) ...[
                const SizedBox(height: 16),
                Text(_error!,
                    style: TextStyle(color: Colors.red.shade700, fontSize: 13)),
              ],
              const SizedBox(height: 24),
              FilledButton(
                onPressed: _guardando ? null : _guardar,
                child: _guardando
                    ? const SizedBox(
                        height: 20, width: 20,
                        child: CircularProgressIndicator(strokeWidth: 2))
                    : const Text('Crear mantenimiento'),
              ),
              const SizedBox(height: 8),
              Text(
                'Queda asignado a vos.',
                textAlign: TextAlign.center,
                style: TextStyle(fontSize: 12, color: Colors.grey.shade600),
              ),
            ],
          );
        },
      ),
    );
  }
}
