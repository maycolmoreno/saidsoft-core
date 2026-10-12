from celery import shared_task

from .services import resincronizar_farmacia_de_vinculados, vincular_activos_por_numero_serie


@shared_task(name='apps.activos.tasks.vincular_activos_por_serie_task')
def vincular_activos_por_serie_task():
    """Diaria (ver CELERY_BEAT_SCHEDULE). Idempotente.

    Dos mitades, y la segunda se agregó el 12-oct-2026 (`INV-04`):

    1. **Vincular** estaciones que todavía no tienen Activo, por número de serie.
    2. **Resincronizar** la farmacia de los que YA están vinculados. Esto faltaba, y por eso
       un equipo trasladado de local quedaba inventariado donde ya no estaba: la vinculación
       por diseño solo mira estaciones sin activo, así que una estación ya vinculada no se
       volvía a evaluar nunca, mientras `registrar_ubicacion_farmacia` se negaba a corregirlo
       a mano diciendo que "se sincroniza sola".

    Van juntas en la misma tarea a propósito: las dos nacen del mismo cruce por serie y
    separarlas daría dos tareas que hacen media cosa cada una.
    """
    vinculados = vincular_activos_por_numero_serie()
    movidos, _ = resincronizar_farmacia_de_vinculados()
    return (
        f'{vinculados} activo(s) vinculado(s) por número de serie; '
        f'{movidos} resincronizado(s) con la farmacia de su estación.'
    )
