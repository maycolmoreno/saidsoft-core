"""
Configuración base compartida por todos los entornos.
Todo valor sensible o dependiente del entorno se lee de variables de entorno (.env).
"""
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DEBUG=(bool, False),
)
# Lee saidsoft/.env si existe (no versionado, ver .env.example)
environ.Env.read_env(BASE_DIR / '.env')

SECRET_KEY = env('SECRET_KEY')
DEBUG = env('DEBUG')
ALLOWED_HOSTS = env.list('ALLOWED_HOSTS', default=['localhost', '127.0.0.1'])

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # terceros
    'rest_framework',
    'rest_framework.authtoken',
    # apps propias
    'apps.catalogo',
    'apps.despliegues',
    'apps.auditoria',
    'apps.mqtt_worker',
    'apps.activos',
    'apps.monitoreo',
    'apps.cuentas',
    'apps.mantenimiento',
    'apps.scripts',
    'apps.cumplimiento',
    'apps.software',
    'apps.integraciones',
    'apps.facturacion',
    'apps.panel',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'apps.cuentas.context_processors.unidad_negocio_contexto',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'
ASGI_APPLICATION = 'config.asgi.application'

# Base de datos: DATABASE_URL en .env. Por defecto, SQLite para desarrollo local
# sin dependencias externas. En producción se usa PostgreSQL (ver produccion.py).
DATABASES = {
    'default': env.db('DATABASE_URL', default=f'sqlite:///{BASE_DIR / "db.sqlite3"}'),
}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# Internacionalización
LANGUAGE_CODE = 'es-EC'
TIME_ZONE = 'America/Guayaquil'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static'] if (BASE_DIR / 'static').exists() else []

MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_URL = 'panel:login'
LOGIN_REDIRECT_URL = 'panel:dashboard'
LOGOUT_REDIRECT_URL = 'panel:login'

# --- Configuración MQTT (panel y worker) ---
MQTT_CONFIG = {
    'HOST': env('MQTT_HOST', default='localhost'),
    'PORT': env.int('MQTT_PORT', default=1883),
    'USERNAME': env('MQTT_USERNAME', default=''),
    'PASSWORD': env('MQTT_PASSWORD', default=''),
    'USE_TLS': env.bool('MQTT_USE_TLS', default=False),
    # CA para validar el cert de EMQX (self-signed en el piloto); vacío = CAs del sistema.
    'CA_CERT': env('MQTT_CA_CERT', default=''),
    'CLIENT_ID_PANEL': env('MQTT_CLIENT_ID_PANEL', default='saidsoft-panel'),
    'CLIENT_ID_WORKER': env('MQTT_CLIENT_ID_WORKER', default='saidsoft-worker'),
}

# API administrativa de EMQX (API Key, no el login del dashboard) — usada por
# apps.mqtt_worker.emqx_admin para darle a cada estación su propia credencial MQTT en el
# enrolamiento, en vez de la única credencial compartida por las ~1.800 estaciones. Vacío
# a propósito por defecto: mientras no se complete (correr el paso nuevo de
# deploy/bootstrap-emqx.sh y pegar la API key acá), el aprovisionamiento por estación
# queda desactivado sin romper el enrolamiento — sigue funcionando con la credencial
# compartida, igual que antes de que existiera este mecanismo.
EMQX_ADMIN_CONFIG = {
    'URL': env('EMQX_API_URL', default=''),
    'API_KEY': env('EMQX_API_KEY', default=''),
    'API_SECRET': env('EMQX_API_SECRET', default=''),
}

# Secreto compartido con el agente para firmar (HMAC-SHA256) el canal de comandos
# MQTT (/saidsof/agente/{codigo}/comando/) — sin esto, cualquiera con acceso de
# publish al broker podría inyectar comandos (incluyendo ejecutar_script).
COMANDO_HMAC_SECRET = env('COMANDO_HMAC_SECRET')

# Clave de cifrado simétrico (Fernet) para datos en reposo que ameritan más que el
# control de acceso normal de Django — hoy solo la clave de recuperación de BitLocker
# (apps.catalogo.models.ClaveRecuperacionBitLocker). Sin default a propósito: generar
# con `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
BITLOCKER_ENCRYPTION_KEY = env('BITLOCKER_ENCRYPTION_KEY')

# URL base desde la que los agentes descargan los archivos de despliegue
# (equivalente a GET_DOMAIN_FILE del sistema anterior)
ARCHIVOS_BASE_URL = env('ARCHIVOS_BASE_URL', default='http://localhost:8000')

# Umbral de error por defecto que detiene automáticamente un despliegue en curso
DESPLIEGUE_UMBRAL_ERROR_PCT_DEFAULT = env.float('DESPLIEGUE_UMBRAL_ERROR_PCT_DEFAULT', default=10.0)

# Distribución en cascada: si está activo, los agentes intentan descargar del caché de
# su farmacia antes que del servidor central (reduce el tráfico VPN a escala).
DESPLIEGUE_USAR_CACHE = env.bool('DESPLIEGUE_USAR_CACHE', default=True)

# Acceso remoto interactivo a estaciones vía un MeshCentral autoalojado (bolt-on,
# no reemplaza el canal MQTT/HMAC de comandos ni el de despliegues). La correlación
# Estacion <-> nodo de MeshCentral es manual (ver apps/catalogo/models.py, campo
# meshcentral_node_id); no hay sincronización automática contra su API todavía.
# Todas las claves tienen default porque, a diferencia de COMANDO_HMAC_SECRET, dev/tests
# no deben romperse antes de tener una instancia de MeshCentral levantada.
MESHCENTRAL_CONFIG = {
    'SERVER_URL': env('MESHCENTRAL_SERVER_URL', default='https://localhost:443'),  # sin slash final
    'MESH_ID': env('MESHCENTRAL_MESH_ID', default=''),  # ID del device group (ver README)
    'AGENT_ARCH_ID': env.int('MESHCENTRAL_AGENT_ARCH_ID', default=4),  # 4 = agente Windows x64
    'INSTALL_FLAGS': env.int('MESHCENTRAL_INSTALL_FLAGS', default=2),  # 2 = solo servicio, sin UI
    # Valores de "viewmode" para saltar directo a escritorio/terminal en el link del
    # dispositivo. Verificados contra una instancia real (6-ago-2026): MeshCentral
    # v1.2.4 usa viewmode=10 para escritorio (no 11, el valor sin confirmar que había
    # antes) y viewmode=12 para terminal.
    'VIEWMODE_ESCRITORIO': env('MESHCENTRAL_VIEWMODE_ESCRITORIO', default='10'),
    'VIEWMODE_TERMINAL': env('MESHCENTRAL_VIEWMODE_TERMINAL', default='12'),
}

# Canal de control de MeshCentral (control.ashx, WebSocket) — usado por
# apps.monitoreo.adapters.meshcentral para el cruce MQTT×MeshCentral (ver
# EstadoDispositivo/evaluar_cruce_monitoreo). Separado de MESHCENTRAL_CONFIG de arriba
# a propósito: eso es solo para armar URLs de relay/instalación, esto es una cuenta
# real de MeshCentral (dedicada, de bajo privilegio — alcanza con acceso de lectura a
# los device groups relevantes) para autenticar el WebSocket. Vacío por defecto: sin
# estas 3 variables, run_meshcentral_worker no arranca, sin romper el resto del stack.
MESHCENTRAL_API_CONFIG = {
    'WS_URL': env('MESHCENTRAL_API_WS_URL', default=''),  # ej. wss://mesh.example.com/control.ashx
    'USUARIO': env('MESHCENTRAL_API_USUARIO', default=''),
    'PASSWORD': env('MESHCENTRAL_API_PASSWORD', default=''),
    # MeshCentral autogenera su propio certificado autofirmado por instancia (no hay un
    # CA fijo versionado como deploy/certs/cert.pem de EMQX) — verificado contra el
    # servidor real de producción (13-ago-2026): sin uno de estos dos, la conexión
    # siempre falla con CERTIFICATE_VERIFY_FAILED, incluso siendo el servidor legítimo.
    # Preferir CA_CERT (fijar el cert real, extraído una vez del servidor) por sobre
    # VERIFICAR_TLS=False (desactiva la verificación por completo).
    'CA_CERT': env('MESHCENTRAL_API_CA_CERT', default=''),
    'VERIFICAR_TLS': env.bool('MESHCENTRAL_API_VERIFICAR_TLS', default=True),
}

# Sondeo SNMP a los Mikrotik de cada farmacia (ver apps.monitoreo.mikrotik) — solo
# consumo total del enlace por sitio, el router no reparte tráfico por estación (sin
# Queues por IP/MAC). Nada de esto se configura por sitio más allá de
# Farmacia.ip_router: ni la community (es el código de la farmacia en minúscula,
# ej. "ml006") ni la interfaz WAN (se resuelve por SNMP contra la ruta por defecto
# activa, no por nombre — confirmado que el nombre no es uniforme entre sitios
# contra un router real de producción, 20-ago-2026). Sin ninguna Farmacia con
# ip_router cargada, la sincronización simplemente no hace nada.
MIKROTIK_SNMP_CONFIG = {
    'PUERTO': env.int('MIKROTIK_SNMP_PUERTO', default=161),
}

# API móvil (apps Flutter): Token Authentication de DRF, sin dependencias externas.
# El técnico obtiene su token una vez en /api/v1/auth/token/ y lo reusa en cada request.
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.TokenAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
}

# --- Celery (fundación async — ver config/celery.py) ---
# Sin Redis local en dev (no hay build oficial de Redis para Windows): CELERY_TASK_ALWAYS_EAGER
# en config/settings/desarrollo.py hace que las tareas corran en el mismo proceso, sin
# necesitar un broker. En producción (deploy/docker-compose.yml) sí hay un `redis` real.
CELERY_BROKER_URL = env('CELERY_BROKER_URL', default='redis://localhost:6379/0')
CELERY_RESULT_BACKEND = env('CELERY_RESULT_BACKEND', default='redis://localhost:6379/0')
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_TRACK_STARTED = True

# Reemplaza el cron externo que requerían estos 4 comandos periódicos (documentado como
# pendiente desde la Fase 1 original del proyecto — nunca se había implementado). El
# nombre de cada entrada es solo una etiqueta legible en el log/dashboard de Celery.
CELERY_BEAT_SCHEDULE = {
    'marcar-estaciones-offline': {
        'task': 'apps.catalogo.tasks.marcar_estaciones_offline_task',
        'schedule': 60.0,  # cada minuto — mismo umbral que el comando manual (5 min sin heartbeat)
    },
    'purgar-metricas-viejas': {
        'task': 'apps.monitoreo.tasks.purgar_metricas_task',
        'schedule': 60.0 * 60 * 24,  # diario
    },
    'purgar-eventos-monitoreo-viejos': {
        'task': 'apps.monitoreo.tasks.purgar_eventos_monitoreo_task',
        'schedule': 60.0 * 60 * 24,  # diario
    },
    'evaluar-cruce-monitoreo': {
        'task': 'apps.monitoreo.tasks.evaluar_cruce_monitoreo_task',
        # Cada 7 min: no hace falta más seguido que marcar-estaciones-offline (60s) —
        # EstadoDispositivo ya se actualiza en tiempo real, esto solo evalúa el cruce.
        'schedule': 60.0 * 7,
    },
    'generar-ejecuciones-programadas': {
        'task': 'apps.scripts.tasks.generar_ejecuciones_programadas_task',
        'schedule': 60.0 * 60 * 24,  # diario — el propio filtro por fecha lo hace seguro de repetir
    },
    'generar-mantenimientos-programados': {
        'task': 'apps.mantenimiento.tasks.generar_mantenimientos_programados_task',
        'schedule': 60.0 * 60 * 24,  # diario
    },
    'generar-escaneos-programados': {
        'task': 'apps.software.tasks.generar_escaneos_programados_task',
        'schedule': 60.0 * 60 * 24,  # diario — el propio filtro por fecha lo hace seguro de repetir
    },
    'vincular-activos-por-serie': {
        'task': 'apps.activos.tasks.vincular_activos_por_serie_task',
        'schedule': 60.0 * 60 * 24,  # diario — un cruce por número de serie no cambia cada minuto
    },
    'escalar-alertas-abiertas': {
        'task': 'apps.monitoreo.tasks.escalar_alertas_task',
        # Cada 10 min: suficiente margen frente a UMBRAL_ESCALAMIENTO_MINUTOS (30) sin
        # sumar carga significativa — mismo orden de magnitud que evaluar-cruce-monitoreo.
        'schedule': 60.0 * 10,
    },
    'sincronizar-ancho-banda-farmacias': {
        'task': 'apps.monitoreo.tasks.sincronizar_ancho_banda_farmacias_task',
        # Cada 5 min: mismo orden de magnitud que las demás tareas de apps.monitoreo.
        # Sin efecto si MIKROTIK_SNMP_CONFIG no está configurado.
        'schedule': 60.0 * 5,
    },
}

# El handler "console" del LOGGING por defecto de Django viene filtrado por
# require_debug_true: con DEBUG=False (producción) los 500 y los logger.exception()
# de la app (ej. apps.mqtt_worker.services, apps.catalogo.services) no llegan a
# ninguna parte salvo que haya ADMINS + email configurados. En un contenedor,
# stdout/stderr ES el log (lo captura `docker compose logs`), así que acá se manda
# todo ahí sin ese filtro.
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',
    },
    'loggers': {
        'django.request': {
            'handlers': ['console'],
            'level': 'ERROR',
            'propagate': False,
        },
    },
}
