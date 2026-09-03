import '../../nucleo/almacen/cola_offline.dart';
import '../../nucleo/red/api.dart';
import 'visita.dart';

class RepoVisitas {
  const RepoVisitas(this._api, {ColaOffline? cola}) : _colaInyectada = cola;

  final Api _api;
  final ColaOffline? _colaInyectada;

  ColaOffline get _cola => _colaInyectada ?? ColaOffline.instancia;

  Future<List<Visita>> listar() async {
    final datos = await _api.obtener('/visitas/') as List;
    return datos
        .map((v) => Visita.desdeJson(Map<String, dynamic>.from(v as Map)))
        .toList();
  }

  /// Marca la llegada. Desde acá corre la ventana contra la que se verifica el GPS.
  Future<bool> iniciar(int id) async {
    try {
      await _api.publicar('/visitas/$id/iniciar/');
      return false;
    } on SinConexion {
      await _cola.encolar(ColaOffline.tipoIniciarVisita, {'id': id});
      return true;
    }
  }

  Future<bool> cerrar(int id, String observaciones) async {
    try {
      await _api.publicar('/visitas/$id/cerrar/', {'observaciones': observaciones});
      return false;
    } on SinConexion {
      await _cola.encolar(
        ColaOffline.tipoCerrarVisita,
        {'id': id, 'observaciones': observaciones},
      );
      return true;
    }
  }
}
