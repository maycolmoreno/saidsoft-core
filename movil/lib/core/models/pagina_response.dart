/// Respuesta genérica de paginación del backend.
class PaginaResponse<T> {
  PaginaResponse({
    required this.contenido,
    required this.paginaActual,
    required this.tamanioPagina,
    required this.totalElementos,
    required this.totalPaginas,
    required this.primera,
    required this.ultima,
  });

  factory PaginaResponse.fromJson(
    Map<String, dynamic> json,
    T Function(Map<String, dynamic>) fromJsonT,
  ) {
    return PaginaResponse(
      contenido: (json['contenido'] as List)
          .map((item) => fromJsonT(Map<String, dynamic>.from(item as Map)))
          .toList(),
      paginaActual: json['paginaActual'] as int,
      tamanioPagina: json['tamanioPagina'] as int,
      totalElementos: json['totalElementos'] as int,
      totalPaginas: json['totalPaginas'] as int,
      primera: json['primera'] as bool,
      ultima: json['ultima'] as bool,
    );
  }

  final List<T> contenido;
  final int paginaActual;
  final int tamanioPagina;
  final int totalElementos;
  final int totalPaginas;
  final bool primera;
  final bool ultima;
}
