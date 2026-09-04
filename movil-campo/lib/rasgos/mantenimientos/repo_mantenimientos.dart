import 'dart:io';

import '../../nucleo/almacen/cola_offline.dart';
import '../../nucleo/red/api.dart';
import 'mantenimiento.dart';

/// Acceso a los mantenimientos del técnico.
///
/// Regla del módulo: toda MUTACIÓN se encola si no hay conexión y devuelve `true`
/// para que la interfaz avise "quedo pendiente"; las LECTURAS propagan el error, con
/// el criterio de que mostrar datos viejos como si fueran de ahora es peor que
/// decirlo.
class RepoMantenimientos {
  const RepoMantenimientos(this._api, {ColaOffline? cola})
      : _colaInyectada = cola;

  final Api _api;
  final ColaOffline? _colaInyectada;

  ColaOffline get _cola => _colaInyectada ?? ColaOffline.instancia;

  Future<List<Mantenimiento>> listar() async {
    final datos = await _api.obtener('/mantenimientos/') as List;
    return datos
        .map((m) => Mantenimiento.desdeJson(Map<String, dynamic>.from(m as Map)))
        .toList();
  }

  Future<Mantenimiento> detalle(int id) async {
    final datos = await _api.obtener('/mantenimientos/$id/') as Map;
    return Mantenimiento.desdeJson(Map<String, dynamic>.from(datos));
  }

  Future<List<ItemChecklist>> checklist(int id) async {
    final datos = await _api.obtener('/mantenimientos/$id/checklist/') as List;
    return datos
        .map((i) => ItemChecklist.desdeJson(Map<String, dynamic>.from(i as Map)))
        .toList();
  }

  /// Marca la llegada. Sin esto el backend no puede medir el SLA de respuesta ni
  /// acotar la ventana contra la que verifica el GPS al cerrar.
  Future<bool> iniciar(int id) async {
    try {
      await _api.publicar('/mantenimientos/$id/iniciar/');
      return false;
    } on SinConexion {
      await _cola.encolar(ColaOffline.tipoIniciar, {'id': id});
      return true;
    }
  }

  Future<bool> marcarChecklist({
    required int mantenimientoId,
    required int actividadId,
    required bool realizada,
  }) async {
    final datos = {
      'mantenimiento_id': mantenimientoId,
      'actividad_id': actividadId,
      'realizada': realizada,
    };
    try {
      await _api.publicar(
        '/mantenimientos/$mantenimientoId/checklist/actualizar/',
        {'actividad_id': actividadId, 'realizada': realizada},
      );
      return false;
    } on SinConexion {
      await _cola.encolar(ColaOffline.tipoChecklist, datos);
      return true;
    }
  }

  Future<bool> firmar({
    required int mantenimientoId,
    required String tipoFirma,
    required String firmaBase64,
  }) async {
    final datos = {
      'mantenimiento_id': mantenimientoId,
      'tipo_firma': tipoFirma,
      'firma_base64': firmaBase64,
    };
    try {
      await _api.publicar('/mantenimientos/$mantenimientoId/firmar/', {
        'tipo_firma': tipoFirma,
        'firma_base64': firmaBase64,
      });
      return false;
    } on SinConexion {
      await _cola.encolar(ColaOffline.tipoFirmar, datos);
      return true;
    }
  }

  /// Las fotos NO se encolan: pueden pesar megabytes y la cola vive en SQLite. Si no
  /// hay red se avisa para que el técnico reintente, en vez de llenar la base del
  /// teléfono con imágenes.
  Future<void> adjuntarFoto(int mantenimientoId, File archivo) {
    return _api.subirArchivo('/mantenimientos/$mantenimientoId/imagenes/', archivo);
  }

  /// Cancela el mantenimiento. NO va por la cola offline a proposito: cancelar es
  /// una decision que el tecnico toma mirando la pantalla, y encolarla dejaria el
  /// equipo aparentemente libre cuando en el servidor sigue trabado.
  Future<void> cancelar({required int mantenimientoId, required String motivo}) {
    return _api.publicar('/mantenimientos/$mantenimientoId/cancelar/', {'motivo': motivo});
  }

  /// Registra un repuesto gastado. Con `bodegaId` descuenta stock real; sin ella solo
  /// deja el costo (repuesto que no salio de bodega).
  ///
  /// Tampoco se encola: si no hay stock, el servidor lo rechaza con un motivo que el
  /// tecnico tiene que ver AHORA para agarrar otro repuesto, no dos horas despues.
  Future<void> registrarRepuesto({
    required int mantenimientoId,
    required int tipoConsumibleId,
    required int cantidad,
    int? bodegaId,
    String? costoUnitario,
  }) {
    return _api.publicar('/mantenimientos/$mantenimientoId/repuestos/', {
      'tipo_consumible': tipoConsumibleId,
      'cantidad': cantidad,
      if (bodegaId != null) 'bodega': bodegaId,
      if (costoUnitario != null && costoUnitario.isNotEmpty) 'costo_unitario': costoUnitario,
    });
  }

  Future<bool> cerrar({
    required int mantenimientoId,
    required String resultadoTecnico,
    int? tiempoRealMinutos,
    String estadoGeneral = '',
  }) async {
    final cuerpo = <String, dynamic>{
      'resultado_tecnico': resultadoTecnico,
      // El backend exige >= 1: mandar 0 daría un 400 en vez de "no se registro".
      if (tiempoRealMinutos != null && tiempoRealMinutos > 0)
        'tiempo_real_minutos': tiempoRealMinutos,
      if (estadoGeneral.isNotEmpty) 'estado_general': estadoGeneral,
    };
    try {
      await _api.publicar('/mantenimientos/$mantenimientoId/cerrar/', cuerpo);
      return false;
    } on SinConexion {
      await _cola.encolar(ColaOffline.tipoCerrar, {
        'mantenimiento_id': mantenimientoId,
        ...cuerpo,
      });
      return true;
    }
  }
}
