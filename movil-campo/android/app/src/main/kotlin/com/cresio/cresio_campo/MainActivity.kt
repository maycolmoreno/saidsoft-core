package com.cresio.cresio_campo

import io.flutter.embedding.android.FlutterFragmentActivity

// FlutterFragmentActivity y no FlutterActivity: el prompt biometrico de Android
// (androidx.biometric, que usa local_auth) necesita un FragmentActivity para
// montarse. Con FlutterActivity el pedido de huella falla en tiempo de ejecucion.
class MainActivity : FlutterFragmentActivity()
