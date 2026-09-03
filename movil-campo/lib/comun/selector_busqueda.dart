import 'package:flutter/material.dart';

import '../nucleo/catalogos.dart';

/// Selector para catálogos LARGOS: se escribe para filtrar en vez de desplegar todo.
///
/// Un `DropdownButtonFormField` construye TODOS sus elementos para resolver cuál está
/// seleccionado. Con 700 farmacias eso es inusable —y en la práctica deja la pantalla
/// trabada—, además de que nadie encuentra un local desplazando una lista de 700.
///
/// Muestra un campo de solo lectura; al tocarlo abre una hoja con buscador y va
/// acotando a medida que se escribe.
class SelectorBusqueda extends StatelessWidget {
  const SelectorBusqueda({
    super.key,
    required this.etiqueta,
    required this.opciones,
    required this.valor,
    required this.onCambio,
    this.ayuda = '',
    this.permiteVacio = true,
    this.textoVacio = 'Todas',
  });

  final String etiqueta;
  final List<Opcion> opciones;
  final String? valor;
  final ValueChanged<String?> onCambio;
  final String ayuda;

  /// Si se puede volver a "sin elegir". Un filtro sí; un campo obligatorio no.
  final bool permiteVacio;
  final String textoVacio;

  Opcion? get _elegida {
    if (valor == null) return null;
    for (final o in opciones) {
      if (o.valor == valor) return o;
    }
    return null;
  }

  @override
  Widget build(BuildContext context) {
    final elegida = _elegida;
    return InkWell(
      onTap: () async {
        final resultado = await showModalBottomSheet<String?>(
          context: context,
          isScrollControlled: true,
          builder: (_) => _HojaBusqueda(
            titulo: etiqueta,
            opciones: opciones,
            permiteVacio: permiteVacio,
            textoVacio: textoVacio,
          ),
        );
        // El sentinel distingue "elegí vaciar" de "cerré sin tocar nada": devolver
        // null en ambos casos borraría la selección cada vez que el técnico se
        // arrepiente y cierra la hoja.
        if (resultado == null) return;
        onCambio(resultado == _vaciar ? null : resultado);
      },
      child: InputDecorator(
        decoration: InputDecoration(
          labelText: etiqueta,
          helperText: ayuda.isEmpty ? null : ayuda,
          helperMaxLines: 2,
          suffixIcon: const Icon(Icons.search),
          isDense: true,
        ),
        child: Text(
          elegida?.etiqueta ?? textoVacio,
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
          style: TextStyle(
            color: elegida == null ? Colors.grey.shade600 : null,
          ),
        ),
      ),
    );
  }
}

/// Valor devuelto cuando se elige explícitamente "sin selección".
const _vaciar = '__vaciar__';

class _HojaBusqueda extends StatefulWidget {
  const _HojaBusqueda({
    required this.titulo,
    required this.opciones,
    required this.permiteVacio,
    required this.textoVacio,
  });

  final String titulo;
  final List<Opcion> opciones;
  final bool permiteVacio;
  final String textoVacio;

  @override
  State<_HojaBusqueda> createState() => _HojaBusquedaState();
}

class _HojaBusquedaState extends State<_HojaBusqueda> {
  final _busqueda = TextEditingController();
  String _termino = '';

  /// Tope de resultados: con el campo vacío no tiene sentido pintar 700 filas, y
  /// escribiendo dos letras ya se acota solo.
  static const _tope = 60;

  @override
  void dispose() {
    _busqueda.dispose();
    super.dispose();
  }

  List<Opcion> get _filtradas {
    final t = _termino.trim().toLowerCase();
    final base = t.isEmpty
        ? widget.opciones
        : widget.opciones
            .where((o) => o.etiqueta.toLowerCase().contains(t))
            .toList();
    return base.length > _tope ? base.sublist(0, _tope) : base;
  }

  @override
  Widget build(BuildContext context) {
    final filtradas = _filtradas;
    final total = _termino.trim().isEmpty
        ? widget.opciones.length
        : widget.opciones
            .where((o) => o.etiqueta.toLowerCase().contains(_termino.trim().toLowerCase()))
            .length;

    return Padding(
      padding: EdgeInsets.only(bottom: MediaQuery.of(context).viewInsets.bottom),
      child: SizedBox(
        height: MediaQuery.of(context).size.height * 0.75,
        child: Column(
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 16, 16, 8),
              child: Row(
                children: [
                  Expanded(
                    child: Text(widget.titulo,
                        style: const TextStyle(
                            fontSize: 16, fontWeight: FontWeight.w700)),
                  ),
                  IconButton(
                    icon: const Icon(Icons.close),
                    onPressed: () => Navigator.of(context).pop(),
                  ),
                ],
              ),
            ),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16),
              child: TextField(
                controller: _busqueda,
                autofocus: true,
                decoration: const InputDecoration(
                  hintText: 'Escribi para buscar',
                  prefixIcon: Icon(Icons.search),
                  isDense: true,
                ),
                onChanged: (v) => setState(() => _termino = v),
              ),
            ),
            if (total > filtradas.length)
              Padding(
                padding: const EdgeInsets.only(top: 8),
                child: Text(
                  'Mostrando ${filtradas.length} de $total. Escribi para acotar.',
                  style: TextStyle(fontSize: 12, color: Colors.grey.shade600),
                ),
              ),
            const SizedBox(height: 8),
            Expanded(
              child: filtradas.isEmpty
                  ? Center(
                      child: Text(
                        'Sin resultados para "${_busqueda.text.trim()}".',
                        style: TextStyle(color: Colors.grey.shade600),
                      ),
                    )
                  : ListView.builder(
                      itemCount: filtradas.length + (widget.permiteVacio ? 1 : 0),
                      itemBuilder: (_, i) {
                        if (widget.permiteVacio && i == 0) {
                          return ListTile(
                            leading: const Icon(Icons.clear),
                            title: Text(widget.textoVacio),
                            onTap: () => Navigator.of(context).pop(_vaciar),
                          );
                        }
                        final o = filtradas[i - (widget.permiteVacio ? 1 : 0)];
                        return ListTile(
                          title: Text(o.etiqueta),
                          onTap: () => Navigator.of(context).pop(o.valor),
                        );
                      },
                    ),
            ),
          ],
        ),
      ),
    );
  }
}
