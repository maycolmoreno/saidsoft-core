import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../nucleo/config.dart';
import '../../nucleo/red/http_seguro.dart';
import 'estado_sesion.dart';

class PantallaServidor extends StatefulWidget {
  const PantallaServidor({super.key});

  @override
  State<PantallaServidor> createState() => _PantallaServidorState();
}

class _PantallaServidorState extends State<PantallaServidor> {
  late final _ip = TextEditingController(text: Config.ip);
  late final _puerto = TextEditingController(text: Config.puerto.toString());
  String? _mensaje;
  bool _ok = false;
  bool _probando = false;

  @override
  void dispose() {
    _ip.dispose();
    _puerto.dispose();
    super.dispose();
  }

  /// Prueba contra `/auth/yo/` SIN token: un 401 es una respuesta VÁLIDA acá —
  /// significa que el servidor está, habla nuestra API y pide credenciales. Un 404
  /// diría que la IP responde pero no es SAIDSOFT.
  ///
  /// Cada modo de falla da un mensaje distinto: un único "no se pudo conectar"
  /// obliga a adivinar si el problema es la red, la IP o el certificado.
  Future<void> _probar() async {
    final puerto = int.tryParse(_puerto.text.trim());
    if (_ip.text.trim().isEmpty || puerto == null) {
      setState(() {
        _ok = false;
        _mensaje = 'Ingresa una IP y un puerto validos.';
      });
      return;
    }

    setState(() {
      _probando = true;
      _mensaje = null;
    });
    try {
      final cliente = await HttpSeguro.cliente();
      final respuesta = await cliente
          .get(Uri.parse('https://${_ip.text.trim()}:$puerto/api/v1/auth/yo/'))
          .timeout(const Duration(seconds: 10));
      setState(() {
        if (respuesta.statusCode == 401 || respuesta.statusCode == 200) {
          _ok = true;
          _mensaje = 'Servidor conectado.';
        } else if (respuesta.statusCode == 404) {
          _ok = false;
          _mensaje = 'Responde, pero no es un servidor SAIDSOFT. Revisa la IP y el puerto.';
        } else {
          _ok = false;
          _mensaje = 'El servidor respondio ${respuesta.statusCode}.';
        }
      });
    } on HandshakeException {
      setState(() {
        _ok = false;
        _mensaje = 'El certificado del servidor no coincide con el de la app. '
            'Puede que se haya regenerado: hace falta una version actualizada.';
      });
    } on TimeoutException {
      setState(() {
        _ok = false;
        _mensaje = 'El servidor no respondio a tiempo. Verifica que estes en la red interna.';
      });
    } on SocketException catch (e) {
      setState(() {
        _ok = false;
        _mensaje = 'No se pudo alcanzar ${_ip.text.trim()}:$puerto '
            '(${e.osError?.message ?? 'sin ruta'}). Verifica la red.';
      });
    } catch (e) {
      setState(() {
        _ok = false;
        _mensaje = 'No se pudo conectar: $e';
      });
    } finally {
      if (mounted) setState(() => _probando = false);
    }
  }

  Future<void> _guardar() async {
    final puerto = int.tryParse(_puerto.text.trim());
    if (_ip.text.trim().isEmpty || puerto == null) return;
    await Config.guardar(_ip.text.trim(), puerto);
    if (!mounted) return;
    context.read<EstadoSesion>().servidorConfigurado();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Servidor')),
      body: ListView(
        padding: const EdgeInsets.all(20),
        children: [
          const Text(
            'Indica el servidor de SAIDSOFT. El telefono tiene que estar en la red interna.',
            style: TextStyle(fontSize: 14),
          ),
          const SizedBox(height: 20),
          TextField(
            controller: _ip,
            decoration: const InputDecoration(labelText: 'IP o host'),
            keyboardType: TextInputType.url,
            autocorrect: false,
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _puerto,
            decoration: const InputDecoration(labelText: 'Puerto'),
            keyboardType: TextInputType.number,
          ),
          if (_mensaje != null) ...[
            const SizedBox(height: 16),
            Text(
              _mensaje!,
              style: TextStyle(
                color: _ok ? Colors.green.shade700 : Colors.red.shade700,
                fontSize: 13,
              ),
            ),
          ],
          const SizedBox(height: 24),
          OutlinedButton.icon(
            onPressed: _probando ? null : _probar,
            icon: _probando
                ? const SizedBox(
                    height: 18, width: 18, child: CircularProgressIndicator(strokeWidth: 2))
                : const Icon(Icons.wifi_tethering),
            label: Text(_probando ? 'Probando...' : 'Probar conexion'),
          ),
          const SizedBox(height: 12),
          FilledButton(
            onPressed: _probando ? null : _guardar,
            child: const Text('Guardar y continuar'),
          ),
        ],
      ),
    );
  }
}
