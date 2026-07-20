from django.contrib import admin

from .models import MuestraMetrica


@admin.register(MuestraMetrica)
class MuestraMetricaAdmin(admin.ModelAdmin):
    list_display = ('estacion', 'timestamp', 'ram_usada_pct', 'cpu_carga_pct', 'temperatura_c', 'latencia_ms')
    list_filter = ('estacion',)
    date_hierarchy = 'timestamp'
    readonly_fields = [f.name for f in MuestraMetrica._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
