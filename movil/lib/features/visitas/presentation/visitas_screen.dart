import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../../core/network/api_client.dart';
import '../data/visitas_repository.dart';

class VisitasScreen extends StatefulWidget {
  const VisitasScreen({super.key});

  @override
  State<VisitasScreen> createState() => _VisitasScreenState();
}

class _VisitasScreenState extends State<VisitasScreen> {
  late final VisitasRepository _repository;
  List<Map<String, dynamic>> _ubicaciones = const [];
  List<Map<String, dynamic>> _custodios = const [];
  List<Map<String, dynamic>> _equipos = const [];
  bool _loading = true;
  int? _ubicacionId;
  int? _custodioId;

  @override
  void initState() {
    super.initState();
    _repository = VisitasRepository(context.read<ApiClient>());
    _loadInitial();
  }

  Future<void> _loadInitial() async {
    setState(() => _loading = true);
    try {
      final ubicaciones = await _repository.listarUbicaciones();
      final custodios = await _repository.listarCustodios();
      final equipos = await _repository.listarEquipos();
      if (!mounted) return;
      setState(() {
        _ubicaciones = ubicaciones;
        _custodios = custodios;
        _equipos = equipos;
      });
    } finally {
      if (mounted) {
        setState(() => _loading = false);
      }
    }
  }

  Future<void> _applyFilters() async {
    setState(() => _loading = true);
    try {
      final custodios =
          await _repository.listarCustodios(ubicacionId: _ubicacionId);
      final equipos = await _repository.listarEquipos(
        ubicacionId: _ubicacionId,
        custodioId: _custodioId,
      );
      if (!mounted) return;
      setState(() {
        _custodios = custodios;
        final custodioExiste =
            _custodios.any((item) => _asInt(item['idCustodio']) == _custodioId);
        if (!custodioExiste) {
          _custodioId = null;
        }
        _equipos = equipos;
      });
    } finally {
      if (mounted) {
        setState(() => _loading = false);
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Visita tecnica')),
      body: RefreshIndicator(
        onRefresh: _loadInitial,
        child: ListView(
          padding: const EdgeInsets.all(16),
          physics: const AlwaysScrollableScrollPhysics(),
          children: [
            DropdownButtonFormField<int?>(
              initialValue: _ubicacionId,
              decoration: const InputDecoration(labelText: 'Ubicacion'),
              items: [
                const DropdownMenuItem<int?>(value: null, child: Text('Todas')),
                ..._ubicaciones.map(
                  (item) => DropdownMenuItem<int?>(
                    value: _asInt(item['id']),
                    child: Text(_text(item['nombre'], fallback: 'Sin nombre')),
                  ),
                ),
              ],
              onChanged: (value) async {
                setState(() {
                  _ubicacionId = value;
                  _custodioId = null;
                });
                await _applyFilters();
              },
            ),
            const SizedBox(height: 12),
            DropdownButtonFormField<int?>(
              initialValue: _custodioId,
              decoration: const InputDecoration(labelText: 'Custodio'),
              items: [
                const DropdownMenuItem<int?>(value: null, child: Text('Todos')),
                ..._custodios.map(
                  (item) => DropdownMenuItem<int?>(
                    value: _asInt(item['idCustodio']),
                    child: Text(
                      '${_text(item['nombre'])} - ${_text(item['area'], fallback: 'Sin area')}',
                    ),
                  ),
                ),
              ],
              onChanged: (value) async {
                setState(() => _custodioId = value);
                await _applyFilters();
              },
            ),
            const SizedBox(height: 16),
            if (_loading)
              const Padding(
                padding: EdgeInsets.symmetric(vertical: 48),
                child: Center(child: CircularProgressIndicator()),
              )
            else if (_equipos.isEmpty)
              const Card(
                child: Padding(
                  padding: EdgeInsets.all(16),
                  child: Text('No hay equipos para los filtros seleccionados.'),
                ),
              )
            else
              ..._equipos.map(
                (item) => Card(
                  child: ListTile(
                    leading: const Icon(Icons.assignment_turned_in_outlined),
                    title:
                        Text(_text(item['codigoSap'], fallback: 'Sin codigo')),
                    subtitle: Text(
                      '${_text(item['marca'])} ${_text(item['modelo'])}\n'
                      'Custodio: ${_text(item['custodioNombre'])}\n'
                      'Ubicacion: ${_text(item['ubicacionNombre'])}',
                    ),
                    isThreeLine: true,
                    trailing: Column(
                      mainAxisAlignment: MainAxisAlignment.center,
                      crossAxisAlignment: CrossAxisAlignment.end,
                      children: [
                        Text(_text(item['estadoMantenimiento'], fallback: '-')),
                        const SizedBox(height: 4),
                        Text(
                          '${_text(item['diasSinMantenimiento'], fallback: '0')} dias',
                          style: Theme.of(context).textTheme.bodySmall,
                        ),
                      ],
                    ),
                  ),
                ),
              ),
          ],
        ),
      ),
    );
  }
}

int? _asInt(dynamic value) {
  if (value is int) {
    return value;
  }
  if (value is num) {
    return value.toInt();
  }
  return int.tryParse(value?.toString() ?? '');
}

String _text(dynamic value, {String fallback = '-'}) {
  final text = value?.toString().trim() ?? '';
  return text.isEmpty ? fallback : text;
}
