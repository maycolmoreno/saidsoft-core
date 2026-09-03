import 'dart:async';

import 'package:flutter/material.dart';
import 'package:geolocator/geolocator.dart';
import 'package:provider/provider.dart';

import '../../comun/tema.dart';
import '../../nucleo/red/api.dart';
import 'repo_gps.dart';

/// Versión del texto que el técnico acepta. Si cambia el texto hay que subirla:
/// el consentimiento se guarda con su versión para poder demostrar QUÉ se aceptó.
const versionTerminos = '1.0';

/// Envío de ubicación durante la jornada.
///
/// El rastreo es EXPLÍCITO y se enciende a mano: la app no manda la posición del
/// técnico por su cuenta. Solo corre con la pantalla abierta (Timer), así que no
/// hace falta el permiso de segundo plano, que es sensible y obliga a justificarlo
/// ante la tienda.
class PantallaGps extends StatefulWidget {
  const PantallaGps({super.key});

  @override
  State<PantallaGps> createState() => _PantallaGpsState();
}

class _PantallaGpsState extends State<PantallaGps> {
  static const _intervalo = Duration(seconds: 30);

  late RepoGps _repo;
  Timer? _temporizador;
  bool _cargando = true;
  bool _consentimiento = false;
  bool _enviando = false;
  String? _error;
  String _ultimo = '';
  int _enviadas = 0;

  @override
  void initState() {
    super.initState();
    _repo = RepoGps(context.read<Api>());
    _cargarConsentimiento();
  }

  @override
  void dispose() {
    _temporizador?.cancel();
    super.dispose();
  }

  Future<void> _cargarConsentimiento() async {
    setState(() {
      _cargando = true;
      _error = null;
    });
    try {
      final aceptado = await _repo.consentimientoRegistrado();
      setState(() => _consentimiento = aceptado);
    } on ErrorApi catch (e) {
      setState(() => _error = e.mensaje);
    } finally {
      if (mounted) setState(() => _cargando = false);
    }
  }

  Future<void> _aceptar() async {
    setState(() => _error = null);
    try {
      await _repo.registrarConsentimiento(versionTerminos: versionTerminos);
      setState(() => _consentimiento = true);
    } on ErrorApi catch (e) {
      setState(() => _error = e.mensaje);
    }
  }

  /// Pide el permiso y confirma que el GPS esté encendido. Sin esto el envío falla
  /// en silencio y el técnico cree que está reportando cuando no.
  Future<bool> _permiso() async {
    if (!await Geolocator.isLocationServiceEnabled()) {
      setState(() => _error = 'Activa la ubicacion del telefono.');
      return false;
    }
    var permiso = await Geolocator.checkPermission();
    if (permiso == LocationPermission.denied) {
      permiso = await Geolocator.requestPermission();
    }
    if (permiso == LocationPermission.denied ||
        permiso == LocationPermission.deniedForever) {
      setState(() => _error = 'Sin permiso de ubicacion no se puede enviar.');
      return false;
    }
    return true;
  }

  Future<void> _alternarEnvio() async {
    if (_enviando) {
      _temporizador?.cancel();
      setState(() {
        _temporizador = null;
        _enviando = false;
      });
      return;
    }
    if (!await _permiso()) return;
    setState(() {
      _enviando = true;
      _error = null;
    });
    await _enviarUna();
    _temporizador = Timer.periodic(_intervalo, (_) => _enviarUna());
  }

  Future<void> _enviarUna() async {
    try {
      final posicion = await Geolocator.getCurrentPosition(
        locationSettings: const LocationSettings(
          accuracy: LocationAccuracy.high,
          timeLimit: Duration(seconds: 15),
        ),
      );
      final pendiente = await _repo.enviarUbicacion(
        latitud: posicion.latitude,
        longitud: posicion.longitude,
        precisionMetros: posicion.accuracy,
        capturadaEn: DateTime.now(),
      );
      if (!mounted) return;
      setState(() {
        _enviadas++;
        _ultimo = pendiente
            ? 'Guardada sin conexion (se envia al recuperar senal)'
            : 'Enviada ${TimeOfDay.now().format(context)}';
      });
    } on ErrorApi catch (e) {
      if (mounted) setState(() => _error = e.mensaje);
    } catch (e) {
      if (mounted) setState(() => _error = 'No se pudo obtener la ubicacion.');
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Mi ubicacion')),
      body: _cargando
          ? const Center(child: CircularProgressIndicator())
          : ListView(
              padding: const EdgeInsets.all(20),
              children: [
                if (!_consentimiento) ...[
                  const Icon(Icons.place_outlined, size: 56, color: Tema.primario),
                  const SizedBox(height: 16),
                  const Text(
                    'Consentimiento de monitoreo',
                    textAlign: TextAlign.center,
                    style: TextStyle(fontSize: 18, fontWeight: FontWeight.w700),
                  ),
                  const SizedBox(height: 16),
                  const Card(
                    child: Padding(
                      padding: EdgeInsets.all(16),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            'Para coordinar el trabajo en campo, la app envia tu '
                            'ubicacion mientras vos actives el envio.',
                          ),
                          SizedBox(height: 12),
                          Text('• Se envia cada 30 segundos.'),
                          Text('• Solo mientras esta pantalla este abierta.'),
                          Text('• Podes detenerlo cuando quieras.'),
                          Text('• Sirve para confirmar tu presencia en la farmacia.'),
                        ],
                      ),
                    ),
                  ),
                  const SizedBox(height: 20),
                  FilledButton.icon(
                    onPressed: _aceptar,
                    icon: const Icon(Icons.check),
                    label: const Text('Acepto el monitoreo'),
                  ),
                ] else ...[
                  Card(
                    child: Padding(
                      padding: const EdgeInsets.all(20),
                      child: Column(
                        children: [
                          Icon(
                            _enviando ? Icons.gps_fixed : Icons.gps_off,
                            size: 48,
                            color: _enviando ? Tema.bien : Colors.grey.shade400,
                          ),
                          const SizedBox(height: 12),
                          Text(
                            _enviando ? 'Enviando ubicacion' : 'Envio detenido',
                            style: const TextStyle(
                                fontSize: 16, fontWeight: FontWeight.w700),
                          ),
                          if (_ultimo.isNotEmpty) ...[
                            const SizedBox(height: 6),
                            Text(_ultimo,
                                style: TextStyle(
                                    fontSize: 12, color: Colors.grey.shade600)),
                          ],
                          if (_enviadas > 0) ...[
                            const SizedBox(height: 4),
                            Text('$_enviadas envio(s) en esta sesion',
                                style: TextStyle(
                                    fontSize: 12, color: Colors.grey.shade600)),
                          ],
                        ],
                      ),
                    ),
                  ),
                  const SizedBox(height: 20),
                  FilledButton.icon(
                    onPressed: _alternarEnvio,
                    style: _enviando
                        ? FilledButton.styleFrom(backgroundColor: Tema.critico)
                        : null,
                    icon: Icon(_enviando ? Icons.stop : Icons.play_arrow),
                    label: Text(_enviando ? 'Detener envio' : 'Comenzar envio'),
                  ),
                  const SizedBox(height: 16),
                  Text(
                    'El envio se detiene si cerras la app o cambias de pantalla.',
                    textAlign: TextAlign.center,
                    style: TextStyle(fontSize: 12, color: Colors.grey.shade600),
                  ),
                ],
                if (_error != null) ...[
                  const SizedBox(height: 20),
                  Text(
                    _error!,
                    textAlign: TextAlign.center,
                    style: TextStyle(color: Colors.red.shade700, fontSize: 13),
                  ),
                ],
              ],
            ),
    );
  }
}
