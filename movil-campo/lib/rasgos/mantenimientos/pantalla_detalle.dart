import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:geolocator/geolocator.dart';
import 'package:image_picker/image_picker.dart';
import 'package:provider/provider.dart';
import 'package:signature/signature.dart';

import '../../comun/tema.dart';
import '../../nucleo/imagen/marca_agua.dart';
import '../../nucleo/red/api.dart';
import '../gps/estado_gps.dart';
import '../sesion/estado_sesion.dart';
import '../sesion/sesion.dart';
import 'mantenimiento.dart';
import 'repo_mantenimientos.dart';

/// El trabajo del técnico en un mantenimiento: llegar, revisar, documentar y cerrar.
class PantallaDetalle extends StatefulWidget {
  const PantallaDetalle({super.key, required this.id});

  final int id;

  @override
  State<PantallaDetalle> createState() => _PantallaDetalleState();
}

class _PantallaDetalleState extends State<PantallaDetalle> {
  late RepoMantenimientos _repo;
  late Future<_Datos> _futuro;
  bool _trabajando = false;

  @override
  void initState() {
    super.initState();
    _repo = RepoMantenimientos(context.read<Api>());
    _futuro = _cargar();
  }

  Future<_Datos> _cargar() async {
    final mantenimiento = await _repo.detalle(widget.id);
    // El checklist solo tiene sentido una vez iniciado; pedirlo siempre igual
    // simplifica y el backend lo devuelve barato.
    final checklist = await _repo.checklist(widget.id);
    return _Datos(mantenimiento, checklist);
  }

  Future<void> _recargar() async {
    final futuro = _cargar();
    setState(() => _futuro = futuro);
    await futuro;
  }

  void _avisar(String texto, {bool pendiente = false}) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(texto),
        backgroundColor: pendiente ? Tema.advertencia : null,
      ),
    );
  }

  /// Envuelve una acción: bloquea la interfaz, traduce el error y recarga.
  /// Sin esto cada botón repetiría el mismo andamiaje y alguno quedaría sin
  /// manejar el caso offline.
  Future<void> _ejecutar(Future<bool> Function() accion, String exito) async {
    setState(() => _trabajando = true);
    try {
      final quedoPendiente = await accion();
      _avisar(
        quedoPendiente
            ? 'Sin conexion: quedo pendiente y se envia solo al recuperar senal.'
            : exito,
        pendiente: quedoPendiente,
      );
      if (!quedoPendiente) await _recargar();
    } on ErrorApi catch (e) {
      _avisar(e.mensaje);
    } finally {
      if (mounted) setState(() => _trabajando = false);
    }
  }

  /// Registra la llegada y arranca el envío de ubicación.
  ///
  /// Encenderlo acá y no dejarlo a mano es lo que hace que la verificación de
  /// presencia sirva: la ventana que el backend mira empieza EN ESTE MOMENTO, así que
  /// un GPS que se enciende después del cierre no aporta ninguna posición y el
  /// mantenimiento queda "sin datos" aunque el técnico haya estado ahí.
  ///
  /// El consentimiento NO se saltea: si falta, se avisa en vez de mandar la posición
  /// igual. Y si el GPS falla, la llegada YA quedó registrada -- no se pierde el
  /// trabajo por un permiso.
  Future<void> _registrarLlegada() async {
    await _ejecutar(() => _repo.iniciar(widget.id), 'Llegada registrada.');
    if (!mounted) return;

    final gps = context.read<EstadoGps>();
    if (!gps.consultado) await gps.cargarConsentimiento();
    if (!mounted) return;

    if (!gps.consentimiento) {
      _avisar(
        'Llegada registrada. Acepta el monitoreo en "Ubicacion" para que se pueda '
        'verificar tu presencia.',
        pendiente: true,
      );
      return;
    }
    if (!gps.enviando && !await gps.comenzar() && mounted) {
      _avisar(gps.error ?? 'No se pudo activar el envio de ubicacion.', pendiente: true);
    }
  }

  Future<void> _adjuntarFoto() async {
    final foto = await ImagePicker().pickImage(
      source: ImageSource.camera,
      // La calidad la define el estampado (encodeJpg en marca_agua), no acá: aplicar
      // dos compresiones seguidas degrada la imagen sin ganar tamaño.
      maxWidth: 2000,
    );
    if (foto == null) return;
    setState(() => _trabajando = true);
    try {
      final posicion = await _ubicacionActual();
      final archivo = await estampar(DatosMarca(
        rutaOrigen: foto.path,
        latitud: posicion?.latitude,
        longitud: posicion?.longitude,
        precisionMetros: posicion?.accuracy,
        momento: DateTime.now(),
      ));
      await _repo.adjuntarFoto(widget.id, archivo);
      _avisar(
        posicion == null
            ? 'Foto adjuntada, pero sin ubicacion: activa el GPS para que quede sellada.'
            : 'Foto adjuntada con la ubicacion sellada.',
        pendiente: posicion == null,
      );
    } on SinConexion {
      // Las fotos no se encolan: pesan y llenarían la base del teléfono.
      _avisar('Sin conexion: la foto no se pudo subir. Reintenta con senal.');
    } on ErrorApi catch (e) {
      _avisar(e.mensaje);
    } finally {
      if (mounted) setState(() => _trabajando = false);
    }
  }

  /// Posición para sellar la foto. Se pide en el momento de la captura y no se reusa
  /// la última del envío periódico: una foto sacada 30 segundos después podría quedar
  /// sellada con coordenadas de otro lado.
  ///
  /// Devuelve null si no hay permiso o el GPS está apagado; la marca lo dice
  /// explícitamente en vez de omitirse.
  Future<Position?> _ubicacionActual() async {
    try {
      if (!await Geolocator.isLocationServiceEnabled()) return null;
      var permiso = await Geolocator.checkPermission();
      if (permiso == LocationPermission.denied) {
        permiso = await Geolocator.requestPermission();
      }
      if (permiso == LocationPermission.denied ||
          permiso == LocationPermission.deniedForever) {
        return null;
      }
      return await Geolocator.getCurrentPosition(
        locationSettings: const LocationSettings(
          accuracy: LocationAccuracy.high,
          timeLimit: Duration(seconds: 12),
        ),
      );
    } catch (_) {
      return null;
    }
  }

  Future<void> _firmar() async {
    final firma = await showModalBottomSheet<String>(
      context: context,
      isScrollControlled: true,
      builder: (_) => const _HojaFirma(),
    );
    if (firma == null || firma.isEmpty) return;
    await _ejecutar(
      () => _repo.firmar(
        mantenimientoId: widget.id,
        tipoFirma: 'custodio',
        firmaBase64: firma,
      ),
      'Firma registrada.',
    );
  }

  Future<void> _cerrar(Mantenimiento m) async {
    final datos = await showModalBottomSheet<_DatosCierre>(
      context: context,
      isScrollControlled: true,
      builder: (_) => const _HojaCierre(),
    );
    if (datos == null) return;
    await _ejecutar(
      () => _repo.cerrar(
        mantenimientoId: widget.id,
        resultadoTecnico: datos.resultado,
        tiempoRealMinutos: datos.minutos,
        estadoGeneral: datos.estadoGeneral,
      ),
      'Mantenimiento cerrado.',
    );
    if (mounted) Navigator.of(context).maybePop();
  }

  @override
  Widget build(BuildContext context) {
    final puedeCerrar = context.watch<EstadoSesion>().puede(Permiso.cerrarMantenimiento);
    return Scaffold(
      appBar: AppBar(title: const Text('Mantenimiento')),
      body: FutureBuilder<_Datos>(
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

          final datos = snap.data;
          if (datos == null) {
            return EstadoMensaje(
              icono: Icons.error_outline,
              titulo: 'No se pudo cargar',
              detalle: 'El mantenimiento llego vacio (estado: ${snap.connectionState.name}).',
              onReintentar: _recargar,
            );
          }
          final m = datos.mantenimiento;
          return Stack(
            children: [
              ListView(
                padding: const EdgeInsets.fromLTRB(16, 16, 16, 100),
                children: [
                  _Cabecera(mantenimiento: m),
                  const SizedBox(height: 16),
                  if (m.enProceso || m.cerrado) ...[
                    _Checklist(
                      items: datos.checklist,
                      habilitado: m.enProceso && !_trabajando,
                      onCambiar: (item, valor) => _ejecutar(
                        () => _repo.marcarChecklist(
                          mantenimientoId: widget.id,
                          actividadId: item.id,
                          realizada: valor,
                        ),
                        valor ? 'Marcado.' : 'Desmarcado.',
                      ),
                    ),
                    const SizedBox(height: 16),
                  ],
                ],
              ),
              if (_trabajando)
                const Positioned.fill(
                  child: ColoredBox(
                    color: Color(0x22000000),
                    child: Center(child: CircularProgressIndicator()),
                  ),
                ),
            ],
          );
        },
      ),
      bottomNavigationBar: FutureBuilder<_Datos>(
        future: _futuro,
        builder: (context, snap) {
          final m = snap.data?.mantenimiento;
          if (m == null || m.cerrado) return const SizedBox.shrink();
          return SafeArea(
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: m.pendiente
                  ? FilledButton.icon(
                      onPressed: _trabajando ? null : _registrarLlegada,
                      icon: const Icon(Icons.play_arrow),
                      label: const Text('Registrar llegada'),
                    )
                  : Row(
                      children: [
                        Expanded(
                          child: OutlinedButton.icon(
                            onPressed: _trabajando ? null : _adjuntarFoto,
                            icon: const Icon(Icons.photo_camera_outlined),
                            label: const Text('Foto'),
                          ),
                        ),
                        const SizedBox(width: 10),
                        Expanded(
                          child: OutlinedButton.icon(
                            onPressed: _trabajando ? null : _firmar,
                            icon: const Icon(Icons.draw_outlined),
                            label: const Text('Firma'),
                          ),
                        ),
                        const SizedBox(width: 10),
                        Expanded(
                          child: FilledButton(
                            onPressed: (_trabajando || !puedeCerrar)
                                ? null
                                : () => _cerrar(m),
                            child: const Text('Cerrar'),
                          ),
                        ),
                      ],
                    ),
            ),
          );
        },
      ),
    );
  }
}

class _Datos {
  const _Datos(this.mantenimiento, this.checklist);
  final Mantenimiento mantenimiento;
  final List<ItemChecklist> checklist;
}

class _Cabecera extends StatelessWidget {
  const _Cabecera({required this.mantenimiento});
  final Mantenimiento mantenimiento;

  @override
  Widget build(BuildContext context) {
    final m = mantenimiento;
    final equipo = m.equipoPrincipal;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              equipo?.codigo ?? 'Sin equipo',
              style: const TextStyle(fontSize: 20, fontWeight: FontWeight.w700),
            ),
            if (equipo != null && equipo.modelo.isNotEmpty)
              Text(equipo.modelo, style: TextStyle(color: Colors.grey.shade700)),
            if (equipo != null && equipo.numeroSerie.isNotEmpty)
              Text('Serie: ${equipo.numeroSerie}',
                  style: TextStyle(fontSize: 12, color: Colors.grey.shade600)),
            const SizedBox(height: 12),
            Wrap(
              spacing: 8,
              runSpacing: 6,
              children: [
                Pastilla(m.etiquetaEstado, color: Tema.primario),
                Pastilla(
                  m.etiquetaPrioridad,
                  color: switch (m.prioridad) {
                    'critica' => Tema.critico,
                    'alta' => Tema.advertencia,
                    'baja' => Tema.neutro,
                    _ => Tema.primario,
                  },
                ),
                if (m.estadoSla != EstadoSla.sinSla && m.restanteSla.isNotEmpty)
                  Pastilla(
                    m.restanteSla,
                    icono: Icons.schedule,
                    color: switch (m.estadoSla) {
                      EstadoSla.incumplido => Tema.critico,
                      EstadoSla.porVencer => Tema.advertencia,
                      _ => Tema.bien,
                    },
                  ),
              ],
            ),
            if (m.descripcion.isNotEmpty) ...[
              const SizedBox(height: 14),
              Text(m.descripcion, style: const TextStyle(fontSize: 14)),
            ],
            if (m.farmacia != null) ...[
              const Divider(height: 28),
              Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Icon(Icons.storefront, size: 18, color: Colors.grey.shade700),
                  const SizedBox(width: 8),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text('${m.farmacia!.codigo} · ${m.farmacia!.nombre}',
                            style: const TextStyle(fontWeight: FontWeight.w600)),
                        if (m.farmacia!.direccion.isNotEmpty)
                          Text(m.farmacia!.direccion,
                              style: TextStyle(
                                  fontSize: 12, color: Colors.grey.shade600)),
                      ],
                    ),
                  ),
                ],
              ),
            ],
            if (m.fechaLegible.isNotEmpty) ...[
              const SizedBox(height: 10),
              Text('Programado: ${m.fechaLegible}',
                  style: TextStyle(fontSize: 12, color: Colors.grey.shade600)),
            ],
          ],
        ),
      ),
    );
  }
}

class _Checklist extends StatelessWidget {
  const _Checklist({
    required this.items,
    required this.habilitado,
    required this.onCambiar,
  });

  final List<ItemChecklist> items;
  final bool habilitado;
  final void Function(ItemChecklist, bool) onCambiar;

  @override
  Widget build(BuildContext context) {
    if (items.isEmpty) {
      return Card(
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Text(
            'No hay actividades de checklist cargadas.',
            style: TextStyle(color: Colors.grey.shade600),
          ),
        ),
      );
    }
    final hechas = items.where((i) => i.realizada).length;
    return Card(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 14, 16, 6),
            child: Row(
              children: [
                const Text('Checklist',
                    style: TextStyle(fontWeight: FontWeight.w700, fontSize: 16)),
                const Spacer(),
                Text('$hechas de ${items.length}',
                    style: TextStyle(fontSize: 12, color: Colors.grey.shade600)),
              ],
            ),
          ),
          for (final item in items)
            CheckboxListTile(
              value: item.realizada,
              onChanged: habilitado ? (v) => onCambiar(item, v ?? false) : null,
              title: Text(item.nombre, style: const TextStyle(fontSize: 14)),
              controlAffinity: ListTileControlAffinity.leading,
              dense: true,
            ),
          const SizedBox(height: 8),
        ],
      ),
    );
  }
}

class _HojaFirma extends StatefulWidget {
  const _HojaFirma();

  @override
  State<_HojaFirma> createState() => _HojaFirmaState();
}

class _HojaFirmaState extends State<_HojaFirma> {
  final _control = SignatureController(penStrokeWidth: 3, penColor: Colors.black);

  @override
  void dispose() {
    _control.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: EdgeInsets.only(
        bottom: MediaQuery.of(context).viewInsets.bottom,
        left: 16,
        right: 16,
        top: 16,
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          const Text('Firma de quien recibe',
              style: TextStyle(fontSize: 16, fontWeight: FontWeight.w700)),
          const SizedBox(height: 12),
          DecoratedBox(
            decoration: BoxDecoration(
              border: Border.all(color: Colors.grey.shade400),
              borderRadius: BorderRadius.circular(10),
            ),
            child: Signature(
              controller: _control,
              height: 200,
              backgroundColor: Colors.white,
            ),
          ),
          const SizedBox(height: 12),
          Row(
            children: [
              Expanded(
                child: OutlinedButton(
                  onPressed: () => _control.clear(),
                  child: const Text('Borrar'),
                ),
              ),
              const SizedBox(width: 10),
              Expanded(
                child: FilledButton(
                  onPressed: () async {
                    if (_control.isEmpty) {
                      Navigator.of(context).pop();
                      return;
                    }
                    final bytes = await _control.toPngBytes();
                    if (!context.mounted) return;
                    // El backend guarda la firma como base64; se manda solo el
                    // contenido, sin el prefijo data: que no espera.
                    Navigator.of(context).pop(
                      bytes == null ? null : base64Encode(bytes),
                    );
                  },
                  child: const Text('Guardar'),
                ),
              ),
            ],
          ),
          const SizedBox(height: 16),
        ],
      ),
    );
  }
}

class _DatosCierre {
  const _DatosCierre(this.resultado, this.minutos, this.estadoGeneral);
  final String resultado;
  final int? minutos;
  final String estadoGeneral;
}

class _HojaCierre extends StatefulWidget {
  const _HojaCierre();

  @override
  State<_HojaCierre> createState() => _HojaCierreState();
}

class _HojaCierreState extends State<_HojaCierre> {
  String? _resultado;
  String? _estadoGeneral;
  final _minutos = TextEditingController();

  @override
  void dispose() {
    _minutos.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: EdgeInsets.only(
        bottom: MediaQuery.of(context).viewInsets.bottom + 16,
        left: 16,
        right: 16,
        top: 16,
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          const Text('Cerrar mantenimiento',
              style: TextStyle(fontSize: 16, fontWeight: FontWeight.w700)),
          const SizedBox(height: 16),
          DropdownButtonFormField<String>(
            initialValue: _resultado,
            isExpanded: true,
            decoration: const InputDecoration(
              labelText: 'Resultado tecnico *',
              helperText: 'Define si el equipo vuelve a bodega o se recomienda la baja.',
              helperMaxLines: 2,
            ),
            items: [
              for (final e in resultadosTecnicos.entries)
                DropdownMenuItem(value: e.key, child: Text(e.value)),
            ],
            onChanged: (v) => setState(() => _resultado = v),
          ),
          const SizedBox(height: 12),
          DropdownButtonFormField<String>(
            initialValue: _estadoGeneral,
            isExpanded: true,
            decoration: const InputDecoration(
              labelText: 'Estado del equipo (opcional)',
            ),
            items: [
              for (final e in estadosGenerales.entries)
                DropdownMenuItem(value: e.key, child: Text(e.value)),
            ],
            onChanged: (v) => setState(() => _estadoGeneral = v),
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _minutos,
            keyboardType: TextInputType.number,
            decoration: const InputDecoration(
              labelText: 'Tiempo real (minutos, opcional)',
            ),
          ),
          const SizedBox(height: 20),
          FilledButton(
            onPressed: _resultado == null
                ? null
                : () => Navigator.of(context).pop(
                      _DatosCierre(
                        _resultado!,
                        int.tryParse(_minutos.text.trim()),
                        _estadoGeneral ?? '',
                      ),
                    ),
            child: const Text('Confirmar cierre'),
          ),
        ],
      ),
    );
  }
}
