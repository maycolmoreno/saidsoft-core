from celery import shared_task

from .models import Mantenimiento
from .services import (
    escalar_cierres_en_conflicto, generar_informe_pdf, generar_mantenimientos_vencidos,
    notificar_mantenimientos_proximos_y_atrasados, purgar_ubicaciones_antiguas,
)


@shared_task(name='apps.mantenimiento.tasks.generar_mantenimientos_programados_task')
def generar_mantenimientos_programados_task():
    """Diaria (ver CELERY_BEAT_SCHEDULE). Segura de repetir en el mismo día: el propio
    filtro por fecha_proximo avanza tras cada generación."""
    total = generar_mantenimientos_vencidos()
    return f'{total} mantenimiento(s) programado(s) generado(s).'


@shared_task(name='apps.mantenimiento.tasks.notificar_mantenimientos_vencimiento_task')
def notificar_mantenimientos_vencimiento_task():
    """Diaria (ver CELERY_BEAT_SCHEDULE). Avisa al técnico asignado de planes próximos a
    vencer y de mantenimientos abiertos hace demasiado tiempo -- idempotente por día."""
    resultado = notificar_mantenimientos_proximos_y_atrasados()
    return f'{resultado["proximos"]} aviso(s) de vencimiento próximo, {resultado["atrasados"]} de atraso.'


@shared_task(name='apps.mantenimiento.tasks.generar_informe_pdf_task')
def generar_informe_pdf_task(mantenimiento_id):
    """Renderiza el informe del mantenimiento y lo deja en `informe_pdf`.

    **No comprueba permisos ni unidad de negocio, a propósito.** Corre en contexto de
    sistema: no hay `request`, ni usuario, ni sesión, así que no hay con qué comparar —
    recibe un id y nada más. Agregarle un `usuario` serviría de poco: el que encola y
    el que después descarga no tienen por qué ser el mismo, y el archivo queda
    guardado en el modelo, no en la respuesta de nadie.

    El control está donde se ENTREGA el archivo, que es donde se decide quién lo ve:
    `apps.panel.views.archivos.mantenimiento_informe` exige sesión, el permiso
    `mantenimiento.view_mantenimiento` y `verificar_acceso` contra
    `Mantenimiento.unidad_negocio`. Y la ruta cruda de `/media/mantenimiento/` está
    cerrada por los dos lados: `internal` en nginx (deploy/nginx/nginx.conf) y
    `PREFIJOS_MEDIA_PROTEGIDOS` en config/urls.py.

    Generar el PDF de un mantenimiento que el solicitante no podría ver no filtra nada
    por sí mismo: el archivo no sale de ahí sin pasar por esa vista.
    """
    mantenimiento = Mantenimiento.objects.get(pk=mantenimiento_id)
    generar_informe_pdf(mantenimiento=mantenimiento)
    return f'Informe PDF generado para mantenimiento #{mantenimiento_id}.'


@shared_task(name='apps.mantenimiento.tasks.escalar_cierres_en_conflicto_task')
def escalar_cierres_en_conflicto_task():
    """Periódica (ver CELERY_BEAT_SCHEDULE). Reenvía el aviso de los cierres de campo en
    conflicto que nadie revisó -- una bandeja que depende de que alguien se acuerde de
    mirarla es una bandeja que no se mira. Segura de repetir: `escalado_en` corta."""
    escalados = escalar_cierres_en_conflicto()
    return f'{escalados} cierre(s) en conflicto escalado(s).'


@shared_task(name='apps.mantenimiento.tasks.purgar_ubicaciones_task')
def purgar_ubicaciones_task():
    """Diaria (ver CELERY_BEAT_SCHEDULE). Retención de las posiciones GPS de los
    técnicos, que hasta el 26-sep-2026 no se purgaban NUNCA -- pese a que el comentario
    de `cerrar_mantenimiento` decía lo contrario. Ver DIAS_RETENCION_UBICACIONES."""
    borradas = purgar_ubicaciones_antiguas()
    return f'{borradas} posicion(es) de tecnico purgada(s).'
