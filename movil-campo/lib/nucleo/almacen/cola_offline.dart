import 'dart:convert';

import 'package:path/path.dart' as p;
import 'package:sqflite/sqflite.dart';

/// Acción que el técnico ejecutó sin conexión y todavía no llegó al servidor.
class AccionPendiente {
  const AccionPendiente({
    required this.id,
    required this.tipo,
    required this.datos,
    required this.creadaEn,
    required this.intentos,
    required this.ultimoError,
    this.enConflicto = false,
    this.mantenimientoId,
  });

  final int id;
  final String tipo;
  final Map<String, dynamic> datos;
  final DateTime creadaEn;
  final int intentos;
  final String ultimoError;

  /// El servidor contesto 409: alguien mas ya movio esto. No se reintenta mas —
  /// reintentar no lo arregla— y pasa a "necesita tu atencion".
  final bool enConflicto;

  /// Sobre que mantenimiento era, para poder mostrar la tarjeta en su pantalla.
  /// Se deriva de `datos`, que segun el tipo lo llama `id` o `mantenimiento_id`.
  final int? mantenimientoId;
}

/// Cola de acciones offline.
///
/// Las farmacias tienen enlaces intermitentes: si el cierre de un mantenimiento se
/// pierde porque no había señal, el técnico rehace el trabajo o —peor— no lo
/// registra. Todo lo que MUTA estado se encola y se reintenta.
///
/// Las lecturas NO se encolan: no tiene sentido diferirlas, y mostrar datos viejos
/// como si fueran nuevos es peor que decir "sin conexion".
class ColaOffline {
  ColaOffline._();
  static final ColaOffline instancia = ColaOffline._();

  static const tipoIniciar = 'iniciar_mantenimiento';
  static const tipoChecklist = 'marcar_checklist';
  static const tipoCerrar = 'cerrar_mantenimiento';
  static const tipoFirmar = 'firmar_mantenimiento';
  static const tipoIniciarVisita = 'iniciar_visita';
  static const tipoCerrarVisita = 'cerrar_visita';
  static const tipoUbicacion = 'enviar_ubicacion';

  Database? _bd;

  Future<Database> get _base async {
    final existente = _bd;
    if (existente != null) return existente;
    final ruta = p.join(await getDatabasesPath(), 'cresio_campo.db');
    final base = await openDatabase(
      ruta,
      // v2: `en_conflicto` separa "reintentar cuando vuelva la senal" de "esto ya no se
      // arregla solo". Sin esa distincion, una accion rechazada por conflicto se
      // reintentaba para siempre y el tecnico veia un contador de pendientes que nunca
      // bajaba, sin saber por que.
      version: 2,
      onCreate: (db, _) => db.execute('''
        CREATE TABLE acciones (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tipo TEXT NOT NULL,
          datos TEXT NOT NULL,
          creada_en TEXT NOT NULL,
          intentos INTEGER NOT NULL DEFAULT 0,
          ultimo_error TEXT NOT NULL DEFAULT '',
          en_conflicto INTEGER NOT NULL DEFAULT 0
        )
      '''),
      onUpgrade: (db, desde, hasta) async {
        // Migracion y no borrar la base: lo que hay adentro es trabajo del tecnico que
        // todavia no llego al servidor.
        if (desde < 2) {
          await db.execute(
            'ALTER TABLE acciones ADD COLUMN en_conflicto INTEGER NOT NULL DEFAULT 0',
          );
        }
      },
    );
    _bd = base;
    return base;
  }

  Future<int> encolar(String tipo, Map<String, dynamic> datos) async {
    final base = await _base;
    return base.insert('acciones', {
      'tipo': tipo,
      'datos': jsonEncode(datos),
      'creada_en': DateTime.now().toUtc().toIso8601String(),
      'intentos': 0,
      'ultimo_error': '',
    });
  }

  /// Lo que todavia se puede subir. Deja afuera lo que quedo en conflicto: reintentar
  /// eso no lo arregla, y mezclarlo haria que el sincronizador choque contra el mismo
  /// 409 en cada corrida.
  Future<List<AccionPendiente>> pendientes() async {
    // En orden de creación: el cierre de un mantenimiento no puede subir antes que
    // su propio "iniciar".
    return _leer(where: 'en_conflicto = 0');
  }

  /// Lo que necesita que el tecnico lo mire: el servidor dijo que ya no aplica.
  Future<List<AccionPendiente>> enConflicto() => _leer(where: 'en_conflicto = 1');

  Future<List<AccionPendiente>> _leer({required String where}) async {
    final base = await _base;
    final filas = await base.query('acciones', where: where, orderBy: 'id ASC');
    return filas.map(_desdeFila).toList();
  }

  AccionPendiente _desdeFila(Map<String, Object?> f) {
    Map<String, dynamic> datos;
    try {
      datos = Map<String, dynamic>.from(jsonDecode(f['datos'] as String) as Map);
    } catch (_) {
      datos = const {};
    }
    // Segun el tipo, el id del mantenimiento viaja como `id` o como `mantenimiento_id`.
    final crudo = datos['mantenimiento_id'] ?? datos['id'];
    return AccionPendiente(
      id: f['id'] as int,
      tipo: f['tipo'] as String,
      datos: datos,
      creadaEn: DateTime.tryParse(f['creada_en'] as String? ?? '') ?? DateTime.now(),
      intentos: f['intentos'] as int? ?? 0,
      ultimoError: f['ultimo_error'] as String? ?? '',
      enConflicto: (f['en_conflicto'] as int? ?? 0) == 1,
      mantenimientoId: crudo is int ? crudo : int.tryParse('$crudo'),
    );
  }

  /// Solo lo que sigue esperando subir. Lo que quedo en conflicto NO cuenta acá: se
  /// muestra aparte y con su motivo, no como un pendiente que nunca baja.
  Future<int> contar() async {
    final base = await _base;
    return Sqflite.firstIntValue(
          await base.rawQuery('SELECT COUNT(*) FROM acciones WHERE en_conflicto = 0'),
        ) ??
        0;
  }

  Future<int> contarEnConflicto() async {
    final base = await _base;
    return Sqflite.firstIntValue(
          await base.rawQuery('SELECT COUNT(*) FROM acciones WHERE en_conflicto = 1'),
        ) ??
        0;
  }

  /// Marca la accion como no reintentable y guarda por que. No se borra: es la unica
  /// constancia en el telefono de que ese trabajo se hizo y de que alguien lo esta
  /// revisando.
  Future<void> marcarEnConflicto(int id, String motivo) async {
    final base = await _base;
    await base.rawUpdate(
      'UPDATE acciones SET en_conflicto = 1, ultimo_error = ? WHERE id = ?',
      [motivo, id],
    );
  }

  /// El tecnico ya vio el conflicto y lo dio por entendido.
  Future<void> descartarConflicto(int id) async {
    final base = await _base;
    await base.delete('acciones', where: 'id = ? AND en_conflicto = 1', whereArgs: [id]);
  }

  Future<void> quitar(int id) async {
    final base = await _base;
    await base.delete('acciones', where: 'id = ?', whereArgs: [id]);
  }

  /// Deja constancia del fallo pero CONSERVA la acción: que el servidor rechace algo
  /// una vez no significa que haya que descartar el trabajo del técnico.
  Future<void> registrarFallo(int id, String error) async {
    final base = await _base;
    await base.rawUpdate(
      'UPDATE acciones SET intentos = intentos + 1, ultimo_error = ? WHERE id = ?',
      [error, id],
    );
  }

  /// Solo para tests.
  Future<void> vaciar() async {
    final base = await _base;
    await base.delete('acciones');
  }
}
