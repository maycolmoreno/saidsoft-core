import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../auth/presentation/auth_provider.dart';
import '../data/planificacion_models.dart';
import 'planificacion_provider.dart';

class PlanificacionFormScreen extends StatefulWidget {
  const PlanificacionFormScreen({super.key});

  @override
  State<PlanificacionFormScreen> createState() =>
      _PlanificacionFormScreenState();
}

class _PlanificacionFormScreenState extends State<PlanificacionFormScreen> {
  final _formKey = GlobalKey<FormState>();
  final _tituloCtrl = TextEditingController();
  final _descripcionCtrl = TextEditingController();
  final _observacionesCtrl = TextEditingController();
  final _tiempoCtrl = TextEditingController();

  String _tipoActividad = 'TAREA_DIARIA';
  String _prioridad = 'MEDIA';
  DateTime _fechaInicio = DateTime.now();
  DateTime _fechaFin = DateTime.now().add(const Duration(days: 1));
  bool _saving = false;

  @override
  void dispose() {
    _tituloCtrl.dispose();
    _descripcionCtrl.dispose();
    _observacionesCtrl.dispose();
    _tiempoCtrl.dispose();
    super.dispose();
  }

  Future<void> _pickDate(bool isStart) async {
    final initial = isStart ? _fechaInicio : _fechaFin;
    final picked = await showDatePicker(
      context: context,
      initialDate: initial,
      firstDate: DateTime(2024),
      lastDate: DateTime(2030),
    );
    if (picked != null) {
      setState(() {
        if (isStart) {
          _fechaInicio = picked;
          if (_fechaFin.isBefore(_fechaInicio)) {
            _fechaFin = _fechaInicio;
          }
        } else {
          _fechaFin = picked;
        }
      });
    }
  }

  String _formatDate(DateTime d) =>
      '${d.year}-${d.month.toString().padLeft(2, '0')}-${d.day.toString().padLeft(2, '0')}';

  Future<void> _guardar() async {
    if (!_formKey.currentState!.validate()) return;

    setState(() => _saving = true);
    final provider = context.read<PlanificacionProvider>();
    final auth = context.read<AuthProvider>();
    final currentUserId = auth.userId ?? 0;
    final actividad = ActividadPlanificada(
      tecnicoId: currentUserId,
      creadoPorId: currentUserId,
      titulo: _tituloCtrl.text.trim(),
      descripcion: _descripcionCtrl.text.trim(),
      tipoActividad: _tipoActividad,
      prioridad: _prioridad,
      fechaInicio: _formatDate(_fechaInicio),
      fechaFin: _formatDate(_fechaFin),
      tiempoEstimadoMinutos:
          _tiempoCtrl.text.isNotEmpty ? int.tryParse(_tiempoCtrl.text) : null,
      observaciones: _observacionesCtrl.text.trim(),
    );

    final success = await provider.crearActividad(actividad);
    if (!mounted) return;
    setState(() => _saving = false);

    if (success) {
      Navigator.pop(context, true);
    } else {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(provider.error ?? 'Error al guardar')),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Nueva Actividad')),
      body: Form(
        key: _formKey,
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            TextFormField(
              controller: _tituloCtrl,
              decoration: const InputDecoration(
                labelText: 'Título *',
                hintText: 'Ej: Revisión equipos piso 3',
              ),
              maxLength: 200,
              validator: (v) =>
                  (v == null || v.trim().isEmpty) ? 'Requerido' : null,
            ),
            const SizedBox(height: 12),
            TextFormField(
              controller: _descripcionCtrl,
              decoration: const InputDecoration(labelText: 'Descripción'),
              maxLines: 3,
            ),
            const SizedBox(height: 12),
            DropdownButtonFormField<String>(
              initialValue: _tipoActividad,
              decoration: const InputDecoration(labelText: 'Tipo de actividad'),
              items: const [
                DropdownMenuItem(
                    value: 'TAREA_DIARIA', child: Text('Tarea diaria')),
                DropdownMenuItem(
                    value: 'TAREA_SEMANAL', child: Text('Tarea semanal')),
                DropdownMenuItem(
                    value: 'MANTENIMIENTO_PROGRAMADO',
                    child: Text('Mantenimiento programado')),
                DropdownMenuItem(
                    value: 'VISITA_TECNICA', child: Text('Visita técnica')),
                DropdownMenuItem(
                    value: 'OBJETIVO_MENSUAL', child: Text('Objetivo mensual')),
              ],
              onChanged: (v) => setState(() => _tipoActividad = v!),
            ),
            const SizedBox(height: 12),
            DropdownButtonFormField<String>(
              initialValue: _prioridad,
              decoration: const InputDecoration(labelText: 'Prioridad'),
              items: const [
                DropdownMenuItem(value: 'BAJA', child: Text('Baja')),
                DropdownMenuItem(value: 'MEDIA', child: Text('Media')),
                DropdownMenuItem(value: 'ALTA', child: Text('Alta')),
                DropdownMenuItem(value: 'URGENTE', child: Text('Urgente')),
              ],
              onChanged: (v) => setState(() => _prioridad = v!),
            ),
            const SizedBox(height: 12),
            Row(
              children: [
                Expanded(
                  child: ListTile(
                    contentPadding: EdgeInsets.zero,
                    title: const Text('Fecha inicio',
                        style: TextStyle(fontSize: 13)),
                    subtitle: Text(_formatDate(_fechaInicio)),
                    trailing: const Icon(Icons.calendar_today, size: 20),
                    onTap: () => _pickDate(true),
                  ),
                ),
                Expanded(
                  child: ListTile(
                    contentPadding: EdgeInsets.zero,
                    title:
                        const Text('Fecha fin', style: TextStyle(fontSize: 13)),
                    subtitle: Text(_formatDate(_fechaFin)),
                    trailing: const Icon(Icons.calendar_today, size: 20),
                    onTap: () => _pickDate(false),
                  ),
                ),
              ],
            ),
            const SizedBox(height: 12),
            TextFormField(
              controller: _tiempoCtrl,
              decoration: const InputDecoration(
                labelText: 'Tiempo estimado (minutos)',
                hintText: 'Ej: 60',
              ),
              keyboardType: TextInputType.number,
            ),
            const SizedBox(height: 12),
            TextFormField(
              controller: _observacionesCtrl,
              decoration: const InputDecoration(labelText: 'Observaciones'),
              maxLines: 2,
            ),
            const SizedBox(height: 24),
            FilledButton.icon(
              onPressed: _saving ? null : _guardar,
              icon: _saving
                  ? const SizedBox(
                      width: 18,
                      height: 18,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  : const Icon(Icons.save),
              label: Text(_saving ? 'Guardando...' : 'Guardar'),
            ),
          ],
        ),
      ),
    );
  }
}
