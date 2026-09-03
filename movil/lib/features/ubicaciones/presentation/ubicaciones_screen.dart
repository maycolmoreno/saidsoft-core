import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../../core/network/api_client.dart';
import '../../auth/data/auth_models.dart';
import '../../auth/presentation/auth_provider.dart';
import '../data/ubicacion_model.dart';
import '../data/ubicaciones_repository.dart';

class UbicacionesScreen extends StatefulWidget {
  const UbicacionesScreen({super.key});

  @override
  State<UbicacionesScreen> createState() => _UbicacionesScreenState();
}

class _UbicacionesScreenState extends State<UbicacionesScreen> {
  final _searchController = TextEditingController();
  String _estado = 'todos';
  late Future<List<Ubicacion>> _future;

  @override
  void initState() {
    super.initState();
    _future = _load();
  }

  @override
  void dispose() {
    _searchController.dispose();
    super.dispose();
  }

  Future<List<Ubicacion>> _load() {
    return UbicacionesRepository(context.read<ApiClient>()).listar();
  }

  Future<void> _reload() async {
    final future = _load();
    setState(() => _future = future);
    await future;
  }

  Future<void> _openForm([Ubicacion? item]) async {
    final canManage = context.read<AuthProvider>().hasCapability(
          UserCapability.manageUbicaciones,
        );
    if (!canManage) {
      return;
    }
    final changed = await Navigator.of(context).push<bool>(
      MaterialPageRoute(
        builder: (_) => UbicacionFormScreen(ubicacion: item),
      ),
    );
    if (changed == true && mounted) {
      await _reload();
    }
  }

  @override
  Widget build(BuildContext context) {
    final canManage = context.watch<AuthProvider>().hasCapability(
          UserCapability.manageUbicaciones,
        );
    return Scaffold(
      appBar: AppBar(title: const Text('Ubicaciones')),
      floatingActionButton: canManage
          ? FloatingActionButton.extended(
              onPressed: () => _openForm(),
              icon: const Icon(Icons.add_location_alt_outlined),
              label: const Text('Nueva'),
            )
          : null,
      body: Column(
        children: [
          TextField(
            controller: _searchController,
            onChanged: (_) => setState(() {}),
            decoration: const InputDecoration(
              hintText: 'Buscar por nombre, agencia o ciudad',
              prefixIcon: Icon(Icons.search),
            ),
          ),
          const SizedBox(height: 12),
          DropdownButtonFormField<String>(
            initialValue: _estado,
            decoration: const InputDecoration(labelText: 'Estado'),
            items: const [
              DropdownMenuItem(value: 'todos', child: Text('Todos')),
              DropdownMenuItem(value: 'activos', child: Text('Activos')),
              DropdownMenuItem(value: 'inactivos', child: Text('Inactivos')),
            ],
            onChanged: (value) => setState(() => _estado = value ?? 'todos'),
          ),
          const SizedBox(height: 16),
          Expanded(
            child: FutureBuilder<List<Ubicacion>>(
              future: _future,
              builder: (context, snapshot) {
                if (snapshot.connectionState == ConnectionState.waiting) {
                  return const Center(child: CircularProgressIndicator());
                }
                if (snapshot.hasError) {
                  return _RetryState(
                    message: 'No fue posible cargar las ubicaciones.',
                    onRetry: _reload,
                  );
                }

                final items = _filter(snapshot.data ?? const []);
                if (items.isEmpty) {
                  return RefreshIndicator(
                    onRefresh: _reload,
                    child: ListView(
                      physics: const AlwaysScrollableScrollPhysics(),
                      children: const [
                        SizedBox(height: 80),
                        Center(child: Text('No hay ubicaciones para mostrar.')),
                      ],
                    ),
                  );
                }

                return RefreshIndicator(
                  onRefresh: _reload,
                  child: ListView.separated(
                    physics: const AlwaysScrollableScrollPhysics(),
                    itemCount: items.length,
                    separatorBuilder: (context, index) =>
                        const SizedBox(height: 12),
                    itemBuilder: (context, index) {
                      final item = items[index];
                      final estado = item.estado;
                      final id = item.id == 0 ? null : item.id;
                      return Card(
                        child: ListTile(
                          onTap: canManage ? () => _openForm(item) : null,
                          leading: Icon(
                            estado
                                ? Icons.location_on_outlined
                                : Icons.location_off_outlined,
                          ),
                          title: Text(item.nombre),
                          subtitle: Text(
                            [
                              'Agencia: ${_text(item.agencia, fallback: 'Sin agencia')}',
                              'Ciudad: ${_text(item.ciudad, fallback: 'Sin ciudad')}',
                              _text(item.direccion, fallback: ''),
                            ].where((part) => part.isNotEmpty).join('\n'),
                          ),
                          isThreeLine: true,
                          trailing: Column(
                            mainAxisAlignment: MainAxisAlignment.center,
                            children: [
                              _EstadoBadge(
                                text: estado ? 'Activo' : 'Inactivo',
                                color: estado ? Colors.green : Colors.red,
                              ),
                              const SizedBox(height: 6),
                              if (canManage)
                                IconButton(
                                  onPressed: id == null
                                      ? null
                                      : () async {
                                          await UbicacionesRepository(
                                                  context.read<ApiClient>())
                                              .actualizarEstado(
                                            idUbicacion: id,
                                            estado: !estado,
                                          );
                                          if (!mounted) return;
                                          await _reload();
                                        },
                                  icon: Icon(
                                    estado
                                        ? Icons.visibility_off_outlined
                                        : Icons.check_circle_outline,
                                  ),
                                ),
                            ],
                          ),
                        ),
                      );
                    },
                  ),
                );
              },
            ),
          ),
        ],
      ),
    );
  }

  List<Ubicacion> _filter(List<Ubicacion> items) {
    final query = _searchController.text.trim().toLowerCase();
    return items.where((item) {
      final estado = item.estado;
      final estadoOk = _estado == 'todos' ||
          (_estado == 'activos' && estado) ||
          (_estado == 'inactivos' && !estado);
      final haystack = [
        _text(item.nombre),
        _text(item.agencia),
        _text(item.ciudad),
        _text(item.direccion),
      ].join(' ').toLowerCase();
      final searchOk = query.isEmpty || haystack.contains(query);
      return estadoOk && searchOk;
    }).toList();
  }
}

class UbicacionFormScreen extends StatefulWidget {
  const UbicacionFormScreen({
    super.key,
    this.ubicacion,
  });

  final Ubicacion? ubicacion;

  @override
  State<UbicacionFormScreen> createState() => _UbicacionFormScreenState();
}

class _UbicacionFormScreenState extends State<UbicacionFormScreen> {
  final _formKey = GlobalKey<FormState>();
  late final TextEditingController _nombreController;
  late final TextEditingController _agenciaController;
  late final TextEditingController _ciudadController;
  late final TextEditingController _direccionController;
  late bool _estado;
  bool _saving = false;

  @override
  void initState() {
    super.initState();
    final item = widget.ubicacion;
    _nombreController = TextEditingController(text: item?.nombre ?? '');
    _agenciaController = TextEditingController(text: item?.agencia ?? '');
    _ciudadController = TextEditingController(text: item?.ciudad ?? '');
    _direccionController = TextEditingController(text: item?.direccion ?? '');
    _estado = item?.estado ?? true;
  }

  @override
  void dispose() {
    _nombreController.dispose();
    _agenciaController.dispose();
    _ciudadController.dispose();
    _direccionController.dispose();
    super.dispose();
  }

  Future<void> _save() async {
    if (!_formKey.currentState!.validate()) {
      return;
    }
    setState(() => _saving = true);
    try {
      final repository = UbicacionesRepository(context.read<ApiClient>());
      final id = widget.ubicacion?.id;
      if (id == null) {
        await repository.crear(
          nombre: _nombreController.text.trim(),
          agencia: _agenciaController.text.trim(),
          ciudad: _ciudadController.text.trim(),
          direccion: _direccionController.text.trim(),
        );
      } else {
        await repository.actualizar(
          idUbicacion: id,
          nombre: _nombreController.text.trim(),
          agencia: _agenciaController.text.trim(),
          ciudad: _ciudadController.text.trim(),
          direccion: _direccionController.text.trim(),
          estado: _estado,
        );
      }
      if (!mounted) return;
      Navigator.of(context).pop(true);
    } catch (_) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('No fue posible guardar la ubicacion.')),
      );
    } finally {
      if (mounted) {
        setState(() => _saving = false);
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final canManage = context.watch<AuthProvider>().hasCapability(
          UserCapability.manageUbicaciones,
        );
    final editing = widget.ubicacion != null;
    return Scaffold(
      appBar:
          AppBar(title: Text(editing ? 'Editar ubicacion' : 'Nueva ubicacion')),
      body: canManage
          ? Form(
              key: _formKey,
              child: ListView(
                padding: const EdgeInsets.all(16),
                children: [
                  TextFormField(
                    controller: _nombreController,
                    decoration: const InputDecoration(labelText: 'Nombre'),
                    validator: (value) => value == null || value.trim().isEmpty
                        ? 'Ingresa el nombre'
                        : null,
                  ),
                  const SizedBox(height: 12),
                  TextFormField(
                    controller: _agenciaController,
                    decoration: const InputDecoration(labelText: 'Agencia'),
                    validator: (value) => value == null || value.trim().isEmpty
                        ? 'Ingresa la agencia'
                        : null,
                  ),
                  const SizedBox(height: 12),
                  TextFormField(
                    controller: _ciudadController,
                    decoration: const InputDecoration(labelText: 'Ciudad'),
                  ),
                  const SizedBox(height: 12),
                  TextFormField(
                    controller: _direccionController,
                    minLines: 2,
                    maxLines: 4,
                    decoration: const InputDecoration(labelText: 'Direccion'),
                  ),
                  const SizedBox(height: 12),
                  SwitchListTile(
                    value: _estado,
                    onChanged: editing
                        ? (value) => setState(() => _estado = value)
                        : null,
                    title: const Text('Ubicacion activa'),
                  ),
                  const SizedBox(height: 20),
                  FilledButton.icon(
                    onPressed: _saving ? null : _save,
                    icon: _saving
                        ? const SizedBox(
                            height: 16,
                            width: 16,
                            child: CircularProgressIndicator(strokeWidth: 2),
                          )
                        : const Icon(Icons.save_outlined),
                    label: Text(_saving ? 'Guardando...' : 'Guardar ubicacion'),
                  ),
                ],
              ),
            )
          : const Center(
              child: Padding(
                padding: EdgeInsets.all(24),
                child: Text(
                  'Tu rol puede consultar ubicaciones, pero no administrarlas desde la app.',
                  textAlign: TextAlign.center,
                ),
              ),
            ),
    );
  }
}

class _EstadoBadge extends StatelessWidget {
  const _EstadoBadge({
    required this.text,
    required this.color,
  });

  final String text;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(999),
      ),
      child: Text(
        text,
        style: TextStyle(
          color: color,
          fontSize: 11,
          fontWeight: FontWeight.w700,
        ),
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

String _text(dynamic value, {String fallback = '-'}) {
  final text = value?.toString().trim() ?? '';
  return text.isEmpty ? fallback : text;
}
