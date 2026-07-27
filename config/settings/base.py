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
    'CLIENT_ID_PANEL': env('MQTT_CLIENT_ID_PANEL', default='saidsoft-panel'),
    'CLIENT_ID_WORKER': env('MQTT_CLIENT_ID_WORKER', default='saidsoft-worker'),
}

# Secreto compartido con el agente para firmar (HMAC-SHA256) el canal de comandos
# MQTT (/saidsof/agente/{codigo}/comando/) — sin esto, cualquiera con acceso de
# publish al broker podría inyectar comandos (incluyendo ejecutar_script).
COMANDO_HMAC_SECRET = env('COMANDO_HMAC_SECRET')

# URL base desde la que los agentes descargan los archivos de despliegue
# (equivalente a GET_DOMAIN_FILE del sistema anterior)
ARCHIVOS_BASE_URL = env('ARCHIVOS_BASE_URL', default='http://localhost:8000')

# Umbral de error por defecto que detiene automáticamente un despliegue en curso
DESPLIEGUE_UMBRAL_ERROR_PCT_DEFAULT = env.float('DESPLIEGUE_UMBRAL_ERROR_PCT_DEFAULT', default=10.0)

# Distribución en cascada: si está activo, los agentes intentan descargar del caché de
# su farmacia antes que del servidor central (reduce el tráfico VPN a escala).
DESPLIEGUE_USAR_CACHE = env.bool('DESPLIEGUE_USAR_CACHE', default=True)

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
