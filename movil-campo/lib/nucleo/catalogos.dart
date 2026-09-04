import 'package:flutter/foundation.dart';

import 'red/api.dart';

/// Opción de un catálogo: un valor que viaja al servidor y una etiqueta que ve el
/// técnico. Sirve tanto para los choices de Django (valor de texto) como para los
/// modelos con id (marca, farmacia), guardando el id como texto.
@immutable
class Opcion {
  const Opcion(this.valor, this.etiqueta);
  final String valor;
  final String etiqueta;

  factory Opcion.desdeChoice(Map<String, dynamic> json) =>
      Opcion(json['valor'].toString(), json['etiqueta'].toString());

  /// Para catálogos con id. `sufijo` agrega el código adelante cuando ayuda a
  /// identificar (una farmacia se reconoce por su código, no solo por el nombre).
  factory Opcion.desdeModelo(Map<String, dynamic> json) {
    final codigo = json['codigo']?.toString();
    final nombre = json['nombre']?.toString() ?? '';
    return Opcion(
      json['id'].toString(),
      codigo == null || codigo.isEmpty ? nombre : '$codigo · $nombre',
    );
  }
}

/// Catálogos de los formularios, traídos en UNA llamada y cacheados en memoria.
///
/// Se piden juntos a propósito: en una farmacia con enlace intermitente, cinco
/// llamadas para llenar un formulario son cinco oportunidades de fallar.
class Catalogos {
  const Catalogos({
    required this.tiposEquipo,
    required this.marcas,
    required this.categorias,
    required this.tiposMantenimiento,
    required this.estadosGenerales,
    required this.prioridades,
    required this.farmacias,
    required this.bodegas,
    required this.colaboradores,
    required this.tiposConsumible,
  });

  final List<Opcion> tiposEquipo;
  final List<Opcion> marcas;
  final List<Opcion> categorias;
  final List<Opcion> tiposMantenimiento;
  final List<Opcion> estadosGenerales;
  final List<Opcion> prioridades;
  final List<Opcion> farmacias;
  final List<Opcion> bodegas;
  final List<Opcion> colaboradores;

  /// Repuestos/consumibles que el tecnico puede declarar como gastados.
  final List<Opcion> tiposConsumible;

  static List<Opcion> _choices(dynamic lista) => (lista as List? ?? const [])
      .map((o) => Opcion.desdeChoice(Map<String, dynamic>.from(o as Map)))
      .toList();

  static List<Opcion> _modelos(dynamic lista) => (lista as List? ?? const [])
      .map((o) => Opcion.desdeModelo(Map<String, dynamic>.from(o as Map)))
      .toList();

  factory Catalogos.desdeJson(Map<String, dynamic> json) => Catalogos(
        tiposEquipo: _choices(json['tipos_equipo']),
        estadosGenerales: _choices(json['estados_generales']),
        prioridades: _choices(json['prioridades']),
        marcas: _modelos(json['marcas']),
        categorias: _modelos(json['categorias']),
        tiposMantenimiento: _modelos(json['tipos_mantenimiento']),
        farmacias: _modelos(json['farmacias']),
        bodegas: _modelos(json['bodegas']),
        colaboradores: _modelos(json['colaboradores']),
        tiposConsumible: _modelos(json['tipos_consumible']),
      );
}

class RepoCatalogos {
  RepoCatalogos(this._api);
  final Api _api;

  Catalogos? _cache;

  /// Los catálogos casi no cambian: se piden una vez por sesión de app. Un cambio en
  /// el panel se ve al reabrir, que para marcas y farmacias es más que suficiente.
  Future<Catalogos> obtener() async {
    final cacheado = _cache;
    if (cacheado != null) return cacheado;
    final datos = await _api.obtener('/catalogos/') as Map;
    final catalogos = Catalogos.desdeJson(Map<String, dynamic>.from(datos));
    _cache = catalogos;
    return catalogos;
  }
}
