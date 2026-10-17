"""Enlace de un solo uso para bajar el instalador de estación desde la propia estación.

`Instalar-Saidsoft.ps1` lleva la clave MQTT compartida, así que no puede ir a `/media/` ni a
un chat. Desde la página de estaciones se pide un enlace, y el técnico pega en la estación
un único comando que lo baja por HTTPS y lo ejecuta. El enlace sirve una vez y por pocos
minutos (`apps.catalogo.instalador`). Ver PLAN_MODERNIZACION.md §10-CG.
"""
from django.conf import settings
from django.contrib.auth.decorators import login_required, permission_required
from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from apps.auditoria.models import ip_cliente, registrar_evento
from apps.catalogo.instalador import (
    MINUTOS_VIGENCIA_ENLACE, ScriptNoDisponible, consumir_enlace, emitir_enlace, generar_script,
)


def _url_base_panel(request) -> str:
    """La URL HTTPS con la que las estaciones alcanzan el panel.

    No sirve `build_absolute_uri`: nginx reenvía `Host` sin el puerto (8084), y el enlace
    saldría apuntando al 443. `CSRF_TRUSTED_ORIGINS` sí lo tiene, y es configuración que ya
    tiene que estar bien para que el panel funcione (sin ella ningún formulario se envía).
    """
    for origen in settings.CSRF_TRUSTED_ORIGINS:
        if origen.startswith('https://'):
            return origen.rstrip('/')
    return request.build_absolute_uri('/').rstrip('/')


@login_required
@permission_required('catalogo.aprobar_estacion', raise_exception=True)
@require_POST
def instalador_enlace_emitir(request):
    # Quien aprueba estaciones es quien decide qué equipo entra a la flota: el mismo
    # permiso alcanza para dar el instalador, que solo sirve para pedir ese ingreso.
    if not settings.MQTT_PASSWORD_AGENTE:
        return render(request, 'panel/_enlace_instalador.html', {
            'error': 'El servidor no tiene MQTT_PASSWORD_AGENTE configurada: no se puede '
                     'armar el instalador. Hay que agregarla al servicio web.',
        })
    enlace, token = emitir_enlace(request.user)
    registrar_evento(usuario=request.user, accion='instalador.emitir', objeto=enlace, request=request)
    url = _url_base_panel(request) + reverse('panel:instalador_descargar', args=[token])
    return render(request, 'panel/_enlace_instalador.html', {
        'url': url, 'enlace': enlace, 'minutos': MINUTOS_VIGENCIA_ENLACE,
    })


@require_GET
@never_cache
def instalador_descargar(request, token):
    """Sin login a propósito: la llama `curl.exe` desde una estación que no tiene sesión.
    Lo que autoriza es el token, que solo existe en el panel de quien lo pidió."""
    if not settings.MQTT_PASSWORD_AGENTE:
        # Antes de consumir: si el servidor no puede armar el script, el enlace no se gasta.
        return HttpResponse('El servidor no puede armar el instalador ahora.\n',
                            status=503, content_type='text/plain; charset=utf-8')
    # El script se arma ANTES de gastar el enlace: si falta el paquete o la versión del
    # agente, el técnico recibe el 503 y su enlace sigue sirviendo cuando se arregle.
    # Antes se consumía primero y el enlace quedaba gastado sin haber entregado nada.
    try:
        script = generar_script(settings.MQTT_PASSWORD_AGENTE)
    except ScriptNoDisponible as exc:
        return HttpResponse(f'{exc}\n', status=503, content_type='text/plain; charset=utf-8')
    enlace = consumir_enlace(token, ip=ip_cliente(request))
    if enlace is None:
        return HttpResponse('Enlace vencido, ya usado o inexistente. Pedi otro en el panel.\n',
                            status=410, content_type='text/plain; charset=utf-8')

    registrar_evento(usuario=enlace.creado_por, accion='instalador.descargar', objeto=enlace,
                     request=request)
    # Con BOM: PowerShell 5.1 lee un .ps1 sin BOM como ANSI y rompe los acentos.
    respuesta = HttpResponse(('﻿' + script).encode('utf-8'),
                             content_type='text/plain; charset=utf-8')
    respuesta['Content-Disposition'] = 'attachment; filename="Instalar-Saidsoft.ps1"'
    return respuesta
