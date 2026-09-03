import 'dart:convert';

import 'package:path/path.dart' as p;
import 'package:path_provider/path_provider.dart';
import 'package:sqflite/sqflite.dart';

class LocalDatabase {
  static final LocalDatabase instance = LocalDatabase._();
  static Database? _database;

  LocalDatabase._();

  Future<Database> get database async {
    _database ??= await _openDatabase();
    return _database!;
  }

  Future<Database> _openDatabase() async {
    final dir = await getApplicationDocumentsDirectory();
    final path = p.join(dir.path, 'cresio_mobile.db');
    return openDatabase(
      path,
      version: 2,
      onCreate: (db, version) async {
        await db.execute('''
          CREATE TABLE mantenimientos_pendientes (
            id TEXT PRIMARY KEY,
            equipo_id INTEGER,
            tipo_mantenimiento TEXT,
            descripcion TEXT,
            odoo_ticket_id TEXT,
            checklist_json TEXT,
            repuestos_json TEXT,
            fecha_creacion TEXT,
            sincronizado INTEGER DEFAULT 0
          )
        ''');
        await db.execute('''
          CREATE TABLE fotos_pendientes (
            id TEXT PRIMARY KEY,
            mantenimiento_local_id TEXT,
            ruta_local TEXT,
            sincronizado INTEGER DEFAULT 0
          )
        ''');
        await db.execute('''
          CREATE TABLE firmas_pendientes (
            id TEXT PRIMARY KEY,
            mantenimiento_local_id TEXT,
            tipo_firma TEXT,
            firma_base64 TEXT,
            sincronizado INTEGER DEFAULT 0
          )
        ''');
        await _createSyncQueue(db);
      },
      onUpgrade: (db, oldVersion, newVersion) async {
        if (oldVersion < 2) {
          await _createSyncQueue(db);
        }
      },
    );
  }

  Future<void> _createSyncQueue(Database db) async {
    await db.execute('''
      CREATE TABLE sync_queue (
        id TEXT PRIMARY KEY,
        operation TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        last_error TEXT
      )
    ''');
  }

  Future<void> insertarMantenimientoPendiente(Map<String, Object?> data) async {
    final db = await database;
    await db.insert(
      'mantenimientos_pendientes',
      data,
      conflictAlgorithm: ConflictAlgorithm.replace,
    );
  }

  Future<List<Map<String, Object?>>> listarPendientes() async {
    final db = await database;
    return db.query(
      'mantenimientos_pendientes',
      where: 'sincronizado = ?',
      whereArgs: [0],
      orderBy: 'fecha_creacion ASC',
    );
  }

  Future<void> marcarSincronizado(String id) async {
    final db = await database;
    await db.update(
      'mantenimientos_pendientes',
      {'sincronizado': 1},
      where: 'id = ?',
      whereArgs: [id],
    );
  }

  Future<int> contarPendientes() async {
    final db = await database;
    final queueResult = await db.rawQuery(
      "SELECT COUNT(*) AS total FROM sync_queue WHERE status = 'pending'",
    );
    final queueCount = Sqflite.firstIntValue(queueResult) ?? 0;
    if (queueCount > 0) {
      return queueCount;
    }
    final result = await db.rawQuery(
      'SELECT COUNT(*) AS total FROM mantenimientos_pendientes WHERE sincronizado = 0',
    );
    return Sqflite.firstIntValue(result) ?? 0;
  }

  Future<void> enqueueSyncOperation({
    required String id,
    required String operation,
    required Map<String, dynamic> payload,
  }) async {
    final db = await database;
    await db.insert(
      'sync_queue',
      {
        'id': id,
        'operation': operation,
        'payload_json': jsonEncode(payload),
        'status': 'pending',
        'attempts': 0,
        'created_at': DateTime.now().toIso8601String(),
        'last_error': null,
      },
      conflictAlgorithm: ConflictAlgorithm.replace,
    );
  }

  Future<List<Map<String, dynamic>>> listarSyncPendientes() async {
    final db = await database;
    final rows = await db.query(
      'sync_queue',
      where: 'status = ?',
      whereArgs: ['pending'],
      orderBy: 'created_at ASC',
    );
    return rows
        .map(
          (row) => {
            ...row,
            'payload': _decodePayload(row['payload_json']),
          },
        )
        .toList();
  }

  Future<void> marcarSyncCompletado(String id) async {
    final db = await database;
    await db.update(
      'sync_queue',
      {
        'status': 'done',
        'attempts': 0,
        'last_error': null,
      },
      where: 'id = ?',
      whereArgs: [id],
    );
  }

  Future<void> registrarSyncError(String id, String error) async {
    final db = await database;
    await db.rawUpdate(
      '''
      UPDATE sync_queue
      SET attempts = attempts + 1,
          last_error = ?,
          status = 'pending'
      WHERE id = ?
      ''',
      [error, id],
    );
  }

  Future<void> marcarSyncFallido(String id) async {
    final db = await database;
    await db.update(
      'sync_queue',
      {'status': 'failed'},
      where: 'id = ?',
      whereArgs: [id],
    );
  }

  Future<int> contarSyncFallidos() async {
    final db = await database;
    final result = await db.rawQuery(
      "SELECT COUNT(*) AS total FROM sync_queue WHERE status = 'failed'",
    );
    return Sqflite.firstIntValue(result) ?? 0;
  }

  dynamic _decodePayload(Object? raw) {
    final text = raw?.toString() ?? '{}';
    return jsonDecode(text);
  }
}
