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
    final destino = destinoDeAccion(accion);
    if (destino == null) {
      // Tipo desconocido (versión vieja de la app): se descarta para que no quede
      // trabado para siempre.
      dev.log('Accion desconocida en la cola: ${accion.tipo}');
      return;
    }
    await _api.publicar(destino.ruta, destino.cuerpo);
  }
}

/// A donde va cada accion encolada y con que cuerpo. `null` = tipo desconocido.
///
/// Funcion aparte y publica a proposito: es la unica parte del sincronizador que
/// decide QUE se manda, y asi se puede probar sin levantar sqflite ni la red.
({String ruta, Map<String, dynamic> cuerpo})? destinoDeAccion(AccionPendiente accion) {
  final d = accion.datos;
  // Hora REAL en que el tecnico hizo la accion, no la de sincronizacion.
  //
  // El backend la usa para fechar el cierre, la firma y la llegada; si falta, cae a
  // su propio reloj (los telefonos sin actualizar no la mandan). Sin esto, un cierre
  // hecho a las 10:00 en una farmacia sin senal y subido a las 18:00 quedaba
  // registrado a las 18:00, corriendo el SLA de resolucion y la ventana contra la que
  // se verifica la presencia por GPS.
  //
  // Mismo patron que `timestamp_captura` en repo_gps.dart, que ya lo hacia bien.
  final ocurridoEn = accion.creadaEn.toUtc().toIso8601String();

  return switch (accion.tipo) {
    ColaOffline.tipoIniciar => (
        ruta: '/mantenimientos/${d['id']}/iniciar/',
        cuerpo: {'ocurrido_en': ocurridoEn},
      ),
    // El checklist NO lleva hora: `registrar_actividad_checklist` hace
    // update_or_create sobre el estado actual, no registra un instante.
    ColaOffline.tipoChecklist => (
        ruta: '/mantenimientos/${d['mantenimiento_id']}/checklist/actualizar/',
        cuerpo: {'actividad_id': d['actividad_id'], 'realizada': d['realizada']},
      ),
    ColaOffline.tipoFirmar => (
        ruta: '/mantenimientos/${d['mantenimiento_id']}/firmar/',
        cuerpo: {
          'tipo_firma': d['tipo_firma'],
          'firma_base64': d['firma_base64'],
          'ocurrido_en': ocurridoEn,
        },
      ),
    ColaOffline.tipoCerrar => (
        ruta: '/mantenimientos/${d['mantenimiento_id']}/cerrar/',
        cuerpo: {
          ...Map<String, dynamic>.from(d)..remove('mantenimiento_id'),
          'ocurrido_en': ocurridoEn,
        },
      ),
    ColaOffline.tipoIniciarVisita => (
        ruta: '/visitas/${d['id']}/iniciar/',
        cuerpo: {'ocurrido_en': ocurridoEn},
      ),
    ColaOffline.tipoCerrarVisita => (
        ruta: '/visitas/${d['id']}/cerrar/',
        cuerpo: {
          'observaciones': d['observaciones'] ?? '',
          'ocurrido_en': ocurridoEn,
        },
      ),
    // La ubicacion ya viajaba con su propia hora (`timestamp_captura`), que es el
    // patron del que salio todo lo de arriba.
    ColaOffline.tipoUbicacion => (ruta: '/ubicaciones-tecnico/', cuerpo: d),
    _ => null,
  };
}
