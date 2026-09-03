import 'package:flutter/material.dart';
import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:provider/provider.dart';

import 'core/config/app_config.dart';
import 'core/network/api_client.dart';
import 'core/storage/local_database.dart';
import 'core/storage/secure_storage_service.dart';
import 'features/auth/data/auth_repository.dart';
import 'features/auth/presentation/auth_provider.dart';
import 'features/auth/presentation/login_screen.dart';
import 'features/configuracion/presentation/server_config_provider.dart';
import 'features/configuracion/presentation/server_config_screen.dart';
import 'features/dashboard/presentation/dashboard_shell.dart';
import 'features/gps/data/gps_repository.dart';
import 'features/gps/presentation/gps_provider.dart';
import 'features/notificaciones/data/push_notificacion_service.dart';
import 'shared/theme/app_theme.dart';

final GlobalKey<NavigatorState> navigatorKey = GlobalKey<NavigatorState>();

/// Handler para mensajes en background (debe ser top-level).
@pragma('vm:entry-point')
Future<void> _firebaseMessagingBackgroundHandler(RemoteMessage message) async {
  await Firebase.initializeApp();
}

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();

  // Inicializar Firebase
  try {
    await Firebase.initializeApp();
    FirebaseMessaging.onBackgroundMessage(_firebaseMessagingBackgroundHandler);
  } catch (_) {
    // Firebase no configurado — push notifications deshabilitadas
  }

  await AppConfig.load();
  await LocalDatabase.instance.database;

  final secureStorage = SecureStorageService();
  late final AuthProvider authProvider;
  late final PushNotificacionService pushService;
  final apiClient = ApiClient(
    secureStorage: secureStorage,
    onUnauthorized: () async {
      try {
        await pushService.limpiarToken();
      } catch (_) {}
      await authProvider.logout();
    },
  );
  authProvider = AuthProvider(
    repository: AuthRepository(
      secureStorage: secureStorage,
    ),
  );
  await authProvider.bootstrap(hasServerConfig: AppConfig.isConfigured);

  pushService = PushNotificacionService(apiClient);

  runApp(
    MultiProvider(
      providers: [
        Provider.value(value: secureStorage),
        Provider.value(value: apiClient),
        Provider.value(value: pushService),
        ChangeNotifierProvider.value(value: authProvider),
        ChangeNotifierProvider(create: (_) => ServerConfigProvider()),
        ChangeNotifierProvider(
          create: (_) => GpsProvider(
            repository: GpsRepository(apiClient: apiClient),
          ),
        ),
      ],
      child: const CresioApp(),
    ),
  );
}

class CresioApp extends StatelessWidget {
  const CresioApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Cresio TICS',
      debugShowCheckedModeBanner: false,
      navigatorKey: navigatorKey,
      theme: AppTheme.light,
      home: Consumer<AuthProvider>(
        builder: (context, auth, _) {
          switch (auth.status) {
            case AuthStatus.loading:
              return const _SplashScreen();
            case AuthStatus.requiresServerConfig:
              return const ServerConfigScreen(showContinue: true);
            case AuthStatus.authenticated:
              return const DashboardShell();
            case AuthStatus.unauthenticated:
              return LoginScreen(message: auth.errorMessage);
          }
        },
      ),
    );
  }
}

class _SplashScreen extends StatelessWidget {
  const _SplashScreen();

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: Container(
        width: double.infinity,
        decoration: const BoxDecoration(
          gradient: LinearGradient(
            begin: Alignment.topCenter,
            end: Alignment.bottomCenter,
            colors: [Color(0xFF2F1857), Color(0xFF47267F), Color(0xFF5C36A0)],
          ),
        ),
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Container(
              width: 80,
              height: 80,
              decoration: BoxDecoration(
                color: Colors.white.withValues(alpha: 0.15),
                borderRadius: BorderRadius.circular(20),
                border: Border.all(
                    color: Colors.white.withValues(alpha: 0.3), width: 2),
              ),
              child: const Icon(Icons.shield_outlined,
                  size: 40, color: Colors.white),
            ),
            const SizedBox(height: 20),
            const Text('CRESIO',
                style: TextStyle(
                    color: Colors.white,
                    fontSize: 32,
                    fontWeight: FontWeight.w800,
                    letterSpacing: 2)),
            const SizedBox(height: 6),
            Text('Gestion de Activos TI',
                style: TextStyle(
                    color: Colors.white.withValues(alpha: 0.8), fontSize: 14)),
            const SizedBox(height: 32),
            const SizedBox(
              width: 24,
              height: 24,
              child: CircularProgressIndicator(
                  strokeWidth: 2.5, color: Colors.white),
            ),
          ],
        ),
      ),
    );
  }
}
