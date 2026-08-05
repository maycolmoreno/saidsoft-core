from .base import *  # noqa: F401,F403

DEBUG = True
ALLOWED_HOSTS = ['*']

EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

# Sin Redis local (Windows no tiene build oficial): las tareas Celery corren en el mismo
# proceso, de forma síncrona, apenas se llaman — sin necesitar un broker corriendo. Sirve
# para desarrollar/testear la lógica de las tareas; no prueba el comportamiento real de
# cola/reintentos, eso solo se valida contra el stack de deploy/docker-compose.yml.
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
