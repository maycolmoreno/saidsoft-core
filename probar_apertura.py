"""Prueba local de la apertura cero-touch, sin broker MQTT.

Llama exactamente a la misma función que invoca el worker al recibir un enrolamiento
(`manejar_enrolamiento`), así que ejercita el camino real de punta a punta salvo el
transporte. Correr con:

    python manage.py shell < probar_apertura.py
"""
from datetime import date

from django.contrib.auth.models import User

from apps.aperturas.models import (
    PasoPlantilla, PerfilEstacionPlantilla, PlantillaApertura, TipoPaso, TipoVerificacion,
)
from apps.aperturas.services import aprobar_apertura, crear_apertura, emitir_tokens, sincronizar_apertura
from apps.catalogo.models import Estacion, Farmacia, Grupo, UnidadNegocio
from apps.mqtt_worker.services import manejar_enrolamiento
from apps.scripts.models import Script

print('\n=== 1. Catálogo mínimo ===')
sg = UnidadNegocio.objects.get(codigo='SG')
grupo, _ = Grupo.objects.get_or_create(codigo='TRX001', defaults={'version_objetivo': '4.2.1'})
farmacia, _ = Farmacia.objects.get_or_create(
    codigo='ML099', defaults={'grupo': grupo, 'unidad_negocio': sg, 'nombre': 'Farmacia de prueba'},
)
creador, _ = User.objects.get_or_create(username='demo-creador')
aprobador, _ = User.objects.get_or_create(username='demo-aprobador')
print(f'Farmacia {farmacia.codigo} ({farmacia.unidad_negocio.codigo}/{farmacia.grupo.codigo})')

# Idempotente: esto es una base de demo descartable, se limpia lo de la corrida anterior
# para poder repetir el guion las veces que haga falta.
Estacion.objects.filter(farmacia=farmacia).delete()
farmacia.aperturas.all().delete()
farmacia.fecha_apertura = None
farmacia.save(update_fields=['fecha_apertura'])

print('\n=== 2. Plantilla de apertura ===')
plantilla, creada = PlantillaApertura.objects.get_or_create(
    unidad_negocio=sg, nombre='Mostrador estándar', version=1,
    defaults={'creado_por': creador, 'descripcion': 'Plantilla de prueba local'},
)
if creada:
    PerfilEstacionPlantilla.objects.create(
        plantilla=plantilla, sufijo='ADM', rol=PerfilEstacionPlantilla.Rol.SERVIDOR,
        monitorear_recursos=True, es_cache_farmacia=True,
    )
    PerfilEstacionPlantilla.objects.create(
        plantilla=plantilla, sufijo='A', rol=PerfilEstacionPlantilla.Rol.CAJA,
    )
    script, _ = Script.objects.get_or_create(
        nombre='Configurar energía (demo)',
        defaults={'contenido': 'Write-Output ok', 'creado_por': creador},
    )
    PasoPlantilla.objects.create(
        plantilla=plantilla, orden=10, nombre='Configurar plan de energía',
        tipo=TipoPaso.SCRIPT, script=script,
    )
    PasoPlantilla.objects.create(
        plantilla=plantilla, orden=20, nombre='Verificar cifrado de disco',
        tipo=TipoPaso.VERIFICACION, verificacion=TipoVerificacion.BITLOCKER,
    )
    PasoPlantilla.objects.create(
        plantilla=plantilla, orden=30, nombre='Alta en Active Directory', tipo=TipoPaso.MANUAL,
    )
for p in plantilla.perfiles_estacion.all():
    print(f'  perfil -{p.sufijo}: {p.get_rol_display()} (monitoreo={p.monitorear_recursos})')
for p in plantilla.pasos.all():
    print(f'  paso {p.orden}: {p.nombre} [{p.get_tipo_display()}]')

print('\n=== 3. Apertura + aprobación (cuatro ojos) + tokens ===')
apertura = crear_apertura(
    farmacia=farmacia, plantilla=plantilla, fecha_prevista=date.today(), usuario=creador,
)
print(f'Creada #{apertura.pk}: {apertura.get_estado_display()}')
try:
    aprobar_apertura(apertura=apertura, usuario=creador)
except ValueError as exc:
    print(f'  (esperado) el creador no puede aprobar: {exc}')
aprobar_apertura(apertura=apertura, usuario=aprobador)
apertura.refresh_from_db()
print(f'Aprobada por {apertura.aprobado_por}: {apertura.get_estado_display()}')

tokens = emitir_tokens(apertura=apertura, usuario=aprobador)
for perfil, plano in tokens:
    print(f'  token para -{perfil.sufijo}: {plano}')

print('\n=== 4. El equipo se enchufa y se enrola con su token ===')
perfil_adm, token_adm = [(p, t) for p, t in tokens if p.sufijo == 'ADM'][0]
respuesta = manejar_enrolamiento({
    'codigo': 'ML099-ADM',
    'hardware_id': 'MACHINE-GUID-DEMO',
    'hostname': 'ML099-ADM',
    'numero_serie': 'SN-DEMO-001',
    'so_nombre': 'Windows 11',
    'so_build': '26100',
    'version_agente': '1.0.0',
    'token_apertura': token_adm,
})
print(f'Respuesta al agente: aceptado={respuesta["aceptado"]}, estado={respuesta["estado_aprobacion"]}')

estacion = Estacion.objects.get(codigo='ML099-ADM')
print(f'Estación {estacion.codigo}:')
print(f'  estado_aprobacion  = {estacion.estado_aprobacion}   <-- sin que nadie la apruebe')
print(f'  monitorear_recursos= {estacion.monitorear_recursos} <-- del perfil')
print(f'  es_cache_farmacia  = {estacion.es_cache_farmacia}   <-- del perfil')

print('\n=== 5. Pasos lanzados ===')
for paso in apertura.pasos.order_by('orden'):
    donde = paso.estacion.codigo if paso.estacion_id else '(sitio)'
    print(f'  [{paso.get_estado_display():<10}] {donde:<12} {paso.nombre} — {paso.detalle[:60]}')

print('\n=== 6. El token no se puede reusar ===')
otra = manejar_enrolamiento({
    'codigo': 'ML099-A', 'hardware_id': 'OTRO-EQUIPO', 'token_apertura': token_adm,
})
print(f'Mismo token, otra estación: aceptado={otra["aceptado"]}, '
      f'estado={otra.get("estado_aprobacion", "-")} <-- cayó al camino manual')

print('\n=== 7. Estado de la apertura ===')
sincronizar_apertura(apertura)
apertura.refresh_from_db()
print(f'Apertura #{apertura.pk}: {apertura.get_estado_display()} '
      f'(falta -A y los pasos abiertos)')
print()
