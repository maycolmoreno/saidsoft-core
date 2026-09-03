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
  });

  final int id;
  final String tipo;
  final Map<String, dynamic> datos;
  final DateTime creadaEn;
  final int intentos;
  final String ultimoError;
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
      version: 1,
      onCreate: (db, _) => db.execute('''
        CREATE TABLE acciones (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tipo TEXT NOT NULL,
          datos TEXT NOT NULL,
          creada_en TEXT NOT NULL,
          intentos INTEGER NOT NULL DEFAULT 0,
          ultimo_error TEXT NOT NULL DEFAULT ''
        )
      '''),
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

  Future<List<AccionPendiente>> pendientes() async {
    final base = await _base;
    // En orden de creación: el cierre de un mantenimiento no puede subir antes que
    // su propio "iniciar".
    final filas = await base.query('acciones', orderBy: 'id ASC');
    return filas.map((f) {
      Map<String, dynamic> datos;
      try {
        datos = Map<String, dynamic>.from(jsonDecode(f['datos'] as String) as Map);
      } catch (_) {
        datos = const {};
      }
      return AccionPendiente(
        id: f['id'] as int,
        tipo: f['tipo'] as String,
        datos: datos,
        creadaEn: DateTime.tryParse(f['creada_en'] as String? ?? '') ?? DateTime.now(),
        intentos: f['intentos'] as int? ?? 0,
        ultimoError: f['ultimo_error'] as String? ?? '',
      );
    }).toList();
  }

  Future<int> contar() async {
    final base = await _base;
    return Sqflite.firstIntValue(
          await base.rawQuery('SELECT COUNT(*) FROM acciones'),
        ) ??
        0;
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
