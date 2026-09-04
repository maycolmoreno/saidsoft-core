import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../comun/selector_busqueda.dart';
import '../../comun/tema.dart';
import '../../nucleo/catalogos.dart';
import '../../nucleo/red/api.dart';

/// Registrar un equipo encontrado en campo.
///
/// Pide farmacia O bodega, no las dos: un técnico que encuentra un equipo sin
/// registrar está parado en la farmacia, y exigirle una bodega lo obligaría a
/// inventar una por la que el equipo nunca pasó.
class PantallaNuevoEquipo extends StatefulWidget {
  const PantallaNuevoEquipo({super.key});

  @override
  State<PantallaNuevoEquipo> createState() => _PantallaNuevoEquipoState();
}

class _PantallaNuevoEquipoState extends State<PantallaNuevoEquipo> {
  late Api _api;
  late Future<Catalogos> _catalogos;

  final _modelo = TextEditingController();
  final _serie = TextEditingController();
  final _procesador = TextEditingController();
  final _ram = TextEditingController();
  final _disco = TextEditingController();

  String? _tipo;
  String? _marca;
  String? _farmacia;
  bool _guardando = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _api = context.read<Api>();
    _catalogos = context.read<RepoCatalogos>().obtener();
  }

  @override
  void dispose() {
    for (final c in [_modelo, _serie, _procesador, _ram, _disco]) {
      c.dispose();
    }
    super.dispose();
  }

  Future<void> _guardar() async {
    if (_tipo == null || _farmacia == null) {
      setState(() => _error = 'Elegi el tipo de equipo y la farmacia donde esta.');
      return;
    }
    setState(() {
      _guardando = true;
      _error = null;
    });
    try {
      final creado = await _api.publicar('/equipos/nuevo/', {
        'tipo': _tipo,
        if (_marca != null) 'marca': int.tryParse(_marca!),
        'modelo': _modelo.text.trim(),
        'numero_serie': _serie.text.trim(),
        'procesador': _procesador.text.trim(),
        // El backend exige >= 1: un 0 daria un 400 en vez de "no se cargo".
        if (int.tryParse(_ram.text.trim()) != null &&
            int.parse(_ram.text.trim()) > 0)
          'ram_gb': int.parse(_ram.text.trim()),
        if (int.tryParse(_disco.text.trim()) != null &&
            int.parse(_disco.text.trim()) > 0)
          'almacenamiento_gb': int.parse(_disco.text.trim()),
        'farmacia': int.tryParse(_farmacia!),
      }) as Map;
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Equipo ${creado['codigo']} registrado.')),
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
      appBar: AppBar(title: const Text('Registrar equipo')),
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
          // Mismo resguardo que en pantalla_nuevo.dart: `snap.data!` sobre un
          // snapshot sin datos lanza, y segun el modo de compilacion eso deja el
          // cuerpo en blanco -- imposible de diagnosticar a distancia.
          final catalogos = snap.data;
          if (catalogos == null) {
            return EstadoMensaje(
              icono: Icons.error_outline,
              titulo: 'No se pudo cargar el formulario',
              detalle: 'El catalogo llego vacio (estado: ${snap.connectionState.name}).',
              onReintentar: () => setState(
                () => _catalogos = context.read<RepoCatalogos>().obtener(),
              ),
            );
          }
          return ListView(
            padding: const EdgeInsets.all(16),
            children: [
              DropdownButtonFormField<String>(
                initialValue: _tipo,
                isExpanded: true,
                decoration: const InputDecoration(labelText: 'Tipo de equipo *'),
                items: [
                  for (final o in catalogos.tiposEquipo)
                    DropdownMenuItem(value: o.valor, child: Text(o.etiqueta)),
                ],
                onChanged: (v) => setState(() => _tipo = v),
              ),
              const SizedBox(height: 12),
              // Con buscador: son 700 farmacias, desplegarlas todas no sirve.
              SelectorBusqueda(
                etiqueta: 'Farmacia donde esta *',
                ayuda: 'Queda registrado como en servicio, sin pasar por bodega.',
                opciones: catalogos.farmacias,
                valor: _farmacia,
                textoVacio: 'Elegi la farmacia',
                permiteVacio: false,
                onCambio: (v) => setState(() => _farmacia = v),
              ),
              const SizedBox(height: 12),
              SelectorBusqueda(
                etiqueta: 'Marca',
                opciones: catalogos.marcas,
                valor: _marca,
                textoVacio: 'Sin marca',
                onCambio: (v) => setState(() => _marca = v),
              ),
              const SizedBox(height: 12),
              TextField(
                controller: _modelo,
                decoration: const InputDecoration(labelText: 'Modelo'),
              ),
              const SizedBox(height: 12),
              TextField(
                controller: _serie,
                decoration: const InputDecoration(
                  labelText: 'Numero de serie',
                  helperText: 'Copialo de la etiqueta del equipo.',
                ),
                autocorrect: false,
              ),

              const SizedBox(height: 24),
              const Text('Especificaciones (opcional)',
                  style: TextStyle(fontWeight: FontWeight.w700)),
              const SizedBox(height: 8),
              TextField(
                controller: _procesador,
                decoration: const InputDecoration(labelText: 'Procesador'),
              ),
              const SizedBox(height: 12),
              Row(
                children: [
                  Expanded(
                    child: TextField(
                      controller: _ram,
                      keyboardType: TextInputType.number,
                      decoration: const InputDecoration(labelText: 'RAM (GB)'),
                    ),
                  ),
                  const SizedBox(width: 12),
                  Expanded(
                    child: TextField(
                      controller: _disco,
                      keyboardType: TextInputType.number,
                      decoration: const InputDecoration(labelText: 'Disco (GB)'),
                    ),
                  ),
                ],
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
                    : const Text('Registrar equipo'),
              ),
            ],
          );
        },
      ),
    );
  }
}
