from django.apps import AppConfig


class MqttWorkerConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.mqtt_worker'
    label = 'mqtt_worker'
    verbose_name = 'Worker MQTT'
