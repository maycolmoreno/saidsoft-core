import 'dart:developer' as dev;

import '../red/api.dart';
import 'cola_offline.dart';

/// Sube al servidor lo que el técnico hizo sin conexión.
///
/// Se ejecuta al abrir la app y al volver la red. Nunca lanza: es trabajo de fondo,
/// y un fallo acá no puede tumbar la pantalla que el técnico está usando.
class Sincronizador {
  Sincronizador(this._api, {ColaOffline? cola}) : _colaInyectada = cola;

  final Api _api;
  final ColaOffline? _colaInyectada;

  ColaOffline get _cola => _colaInyectada ?? ColaOffline.instancia;

  /// Devuelve cuántas acciones se subieron. Se detiene ante la primera falta de
  /// conexión: si no hay red, insistir con el resto solo gasta batería.
  Future<int> sincronizar() async {
    var subidas = 0;
    for (final accion in await _cola.pendientes()) {
      try {
        await _ejecutar(accion);
        await _cola.quitar(accion.id);
        subidas++;
      } on SinConexion {
        break;
      } on SesionExpirada {
        // Sin sesión no se puede subir nada: se conserva todo para el próximo login.
        break;
      } on ErrorApi catch (e) {
        // El servidor la rechazó (datos inválidos, ya cerrado, sin permiso). Se
        // conserva con el motivo: descartar trabajo del técnico en silencio sería
        // peor que dejarlo pendiente y visible.
        await _cola.registrarFallo(accion.id, e.mensaje);
        dev.log('Accion ${accion.id} (${accion.tipo}) rechazada: ${e.mensaje}');
      } catch (e) {
        await _cola.registrarFallo(accion.id, e.toString());
      }
    }
    return subidas;
  }

  Future<void> _ejecutar(AccionPendiente accion) async {
    final d = accion.datos;
    switch (accion.tipo) {
      case ColaOffline.tipoIniciar:
        await _api.publicar('/mantenimientos/${d['id']}/iniciar/');
      case ColaOffline.tipoChecklist:
        await _api.publicar(
          '/mantenimientos/${d['mantenimiento_id']}/checklist/actualizar/',
          {'actividad_id': d['actividad_id'], 'realizada': d['realizada']},
        );
      case ColaOffline.tipoFirmar:
        await _api.publicar('/mantenimientos/${d['mantenimiento_id']}/firmar/', {
          'tipo_firma': d['tipo_firma'],
          'firma_base64': d['firma_base64'],
        });
      case ColaOffline.tipoCerrar:
        final cuerpo = Map<String, dynamic>.from(d)..remove('mantenimiento_id');
        await _api.publicar(
          '/mantenimientos/${d['mantenimiento_id']}/cerrar/',
          cuerpo,
        );
      case ColaOffline.tipoIniciarVisita:
        await _api.publicar('/visitas/${d['id']}/iniciar/');
      case ColaOffline.tipoCerrarVisita:
        await _api.publicar('/visitas/${d['id']}/cerrar/', {
          'observaciones': d['observaciones'] ?? '',
        });
      case ColaOffline.tipoUbicacion:
        await _api.publicar('/ubicaciones-tecnico/', d);
      default:
        // Tipo desconocido (versión vieja de la app): se descarta para que no quede
        // trabado para siempre.
        dev.log('Accion desconocida en la cola: ${accion.tipo}');
    }
  }
}
