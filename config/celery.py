"""Integración estándar Django+Celery. Fundación async del proyecto (Fase 1 del
roadmap "IT Operations Platform"): condición previa para Odoo, PDF, notificaciones y
para reemplazar las 4 tareas periódicas que hoy dependían de cron externo (ver
CELERY_BEAT_SCHEDULE en config/settings/base.py).
"""
import os

from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.desarrollo')

app = Celery('saidsoft')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()
