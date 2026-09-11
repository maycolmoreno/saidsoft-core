"""Orquestación de una apertura autoprovisionada.

Sigue el mismo patrón que apps/despliegues/services.py y apps/scripts/services.py: la
lógica vive acá, separada de las vistas, y el trabajo real se delega en los módulos que
ya lo hacen. Este archivo no habla MQTT directamente ni arma payloads de comando — para
eso llama a `apps.scripts.services`, `apps.software.services` y
`apps.despliegues.services`.

El punto de entrada que importa es `enrolar_estacion_de_apertura`: lo llama
`apps.mqtt_worker.services.manejar_enrolamiento` cuando un agente se presenta con un
token de apertura válido. Todo lo demás (crear, aprobar, emitir tokens) lo dispara una
persona desde el panel.
"""
import logging

from django.db import transaction
from django.utils import timezone

from apps.auditoria.models import registrar_evento
from apps.catalogo.models import Estacion

from .models import (
    Apertura,
    EventoApertura,
    PasoApertura,
    TipoPaso,
    TipoVerificacion,
    TokenApertura,
    hashear_token,
)

logger = logging.getLogger(__name__)


def _registrar(paso, evento, detalle=''):
    return EventoApertura.objects.create(paso=paso, evento=evento, detalle=detalle)


# --------------------------------------------------------------------------------------
# Ciclo de vida de la apertura (lo dispara una persona desde el panel)
# --------------------------------------------------------------------------------------

def crear_apertura(*, farmacia, plantilla, fecha_prevista, usuario, observacion=''):
    """Crea la apertura y materializa sus pasos manuales.

    Los pasos de estación NO se crean acá: las estaciones de una farmacia nueva todavía
    no existen en la base. Se materializan cuando cada equipo se enrola con su token
    (ver `enrolar_estacion_de_apertura`).
    """
    if plantilla.unidad_negocio_id != farmacia.unidad_negocio_id:
        raise ValueError(
            f'La plantilla es de {plantilla.unidad_negocio.codigo} y la farmacia de '
            f'{farmacia.unidad_negocio.codigo}: una apertura no cruza unidades de negocio.',
        )
    if not plantilla.activa:
        raise ValueError('Esa plantilla está desactivada.')
    vigente = Apertura.objects.filter(farmacia=farmacia, estado__in=Apertura.ESTADOS_VIGENTES).first()
    if vigente is not None:
        raise ValueError(f'{farmacia.codigo} ya tiene una apertura vigente (#{vigente.pk}).')

    with transaction.atomic():
        apertura = Apertura.objects.create(
            farmacia=farmacia, plantilla=plantilla, plantilla_version=plantilla.version,
            fecha_prevista=fecha_prevista, observacion=observacion, creado_por=usuario,
            estado=Apertura.Estado.PENDIENTE_APROBACION,
        )
        for paso_plantilla in plantilla.pasos.filter(tipo=TipoPaso.MANUAL):
            paso = PasoApertura.objects.create(
                apertura=apertura, paso_plantilla=paso_plantilla, estacion=None,
                orden=paso_plantilla.orden, nombre=paso_plantilla.nombre, tipo=paso_plantilla.tipo,
            )
            _registrar(paso, EventoApertura.Paso.CREADO, 'Paso manual del sitio')

    registrar_evento(
        usuario=usuario, accion='apertura.crear', objeto=apertura,
        detalle={'plantilla': str(plantilla), 'farmacia': farmacia.codigo},
    )
    return apertura


def aprobar_apertura(*, apertura, usuario):
    """Regla de cuatro ojos, igual que `aprobar_ejecucion_script` y los despliegues."""
    if apertura.estado != Apertura.Estado.PENDIENTE_APROBACION:
        raise ValueError('Esta apertura ya no está pendiente de aprobación.')
    if apertura.creado_por_id == usuario.id:
        raise ValueError('Quien crea la apertura no puede aprobarla (regla de cuatro ojos).')
    apertura.aprobado_por = usuario
    apertura.estado = Apertura.Estado.APROBADA
    apertura.fecha_aprobacion = timezone.now()
    apertura.save(update_fields=['aprobado_por', 'estado', 'fecha_aprobacion'])
    registrar_evento(usuario=usuario, accion='apertura.aprobar', objeto=apertura)
    return apertura


def emitir_tokens(*, apertura, usuario, dias_vigencia=None):
    """Emite un token por cada perfil de estación que todavía no tenga uno vigente o usado.

    Devuelve `[(perfil, token_en_claro), ...]`. Los valores en claro solo existen en esta
    respuesta: no se persisten ni vuelven a estar disponibles (ver `TokenApertura`).

    No se emite sobre una apertura sin aprobar: el token auto-aprueba estaciones, así que
    emitirlo antes de la aprobación sería saltear justamente el control que la aprobación
    representa.
    """
    if apertura.estado not in (Apertura.Estado.APROBADA, Apertura.Estado.EN_CURSO):
        raise ValueError('Solo se emiten tokens de una apertura aprobada.')

    ya_cubiertos = {
        t.perfil_id for t in apertura.tokens.all() if t.vigente or t.usado_en is not None
    }
    emitidos = []
    kwargs = {'dias_vigencia': dias_vigencia} if dias_vigencia else {}
    for perfil in apertura.plantilla.perfiles_estacion.all():
        if perfil.pk in ya_cubiertos:
            continue
        token, token_plano = TokenApertura.emitir(
            apertura=apertura, perfil=perfil, usuario=usuario, **kwargs,
        )
        emitidos.append((perfil, token_plano))

    if emitidos:
        registrar_evento(
            usuario=usuario, accion='apertura.emitir_tokens', objeto=apertura,
            # El detalle de auditoría nunca lleva el secreto: solo a qué perfiles se emitió.
            detalle={'perfiles': [p.sufijo for p, _ in emitidos]},
        )
    return emitidos


def revocar_token(*, token, usuario, motivo=''):
    token.revocado = True
    token.save(update_fields=['revocado'])
    registrar_evento(
        usuario=usuario, accion='apertura.revocar_token', objeto=token.apertura,
        detalle={'prefijo': token.prefijo, 'perfil': token.perfil.sufijo, 'motivo': motivo},
    )
    return token


# --------------------------------------------------------------------------------------
# Enrolamiento por token (lo dispara el agente, vía apps.mqtt_worker)
# --------------------------------------------------------------------------------------

def consumir_token(*, token_plano, codigo_estacion, hardware_id):
    """Valida el token contra el código que dice traer el agente.

    Devuelve el `TokenApertura` si todo cierra, o None. Devolver None (y no una excepción)
    es a propósito: el llamador es el worker MQTT, que ante un token inválido debe seguir
    por el camino normal (estación pendiente de aprobación manual), no romperse.

    Las tres cosas que se exigen, y por qué:

    - Que el token esté vigente: un solo uso, sin revocar y sin vencer.
    - Que el sufijo del código coincida con el perfil al que se emitió. Sin esto, un token
      emitido para la caja `-B` podría enrolar un equipo que dice ser el servidor `-ADM`,
      y llevarse los flags de ese perfil.
    - Que la farmacia del código sea la de la apertura. Sin esto, un token filtrado sirve
      para enrolar un equipo en cualquier farmacia de la cadena.
    """
    if not token_plano or not codigo_estacion:
        return None

    token = (
        TokenApertura.objects
        .select_related('apertura__farmacia', 'perfil')
        .filter(token_hash=hashear_token(token_plano))
        .first()
    )
    if token is None:
        logger.warning('Enrolamiento con token de apertura desconocido para %s', codigo_estacion)
        return None
    if not token.vigente:
        logger.warning(
            'Enrolamiento rechazado para %s: token de apertura %s (%s)',
            codigo_estacion, token.prefijo, token.motivo_no_vigente,
        )
        return None
    if token.apertura.estado not in (Apertura.Estado.APROBADA, Apertura.Estado.EN_CURSO):
        logger.warning(
            'Enrolamiento rechazado para %s: la apertura #%s está en estado %s',
            codigo_estacion, token.apertura_id, token.apertura.estado,
        )
        return None

    codigo_farmacia, _, sufijo = codigo_estacion.partition('-')
    if codigo_farmacia != token.apertura.farmacia.codigo:
        logger.warning(
            'Enrolamiento rechazado: el token es de %s y el equipo dice ser %s',
            token.apertura.farmacia.codigo, codigo_estacion,
        )
        return None
    if sufijo != token.perfil.sufijo:
        logger.warning(
            'Enrolamiento rechazado: el token es para el perfil -%s y el equipo dice ser %s',
            token.perfil.sufijo, codigo_estacion,
        )
        return None
    return token


def enrolar_estacion_de_apertura(*, token, codigo, payload):
    """Crea la estación YA APROBADA con la configuración de su perfil y lanza sus pasos.

    Esta es la única vía por la que una estación entra a la flota sin que una persona la
    apruebe en el panel. Lo que la respalda es el token: de un solo uso, con vencimiento,
    atado a una farmacia y a un perfil concretos, emitido por alguien con permiso sobre una
    apertura ya aprobada por un segundo usuario.

    La estación no desaparece de la vista de nadie por entrar así: queda el
    `EventoApertura` de su enrolamiento y el evento de auditoría `apertura.estacion_enrolada`
    con el prefijo del token que la autorizó.
    """
    apertura = token.apertura
    perfil = token.perfil

    with transaction.atomic():
        estacion = Estacion.objects.create(
            codigo=codigo,
            farmacia=apertura.farmacia,
            hardware_id=payload.get('hardware_id', ''),
            hostname=payload.get('hostname', ''),
            numero_serie=payload.get('numero_serie', ''),
            so_nombre=payload.get('so_nombre', ''),
            so_build=payload.get('so_build', ''),
            version_agente=payload.get('version_agente', ''),
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            monitorear_recursos=perfil.monitorear_recursos,
            es_cache_farmacia=perfil.es_cache_farmacia,
        )
        token.usado_en = timezone.now()
        token.estacion = estacion
        token.hardware_id = payload.get('hardware_id', '')
        token.save(update_fields=['usado_en', 'estacion', 'hardware_id'])

        if apertura.estado == Apertura.Estado.APROBADA:
            apertura.estado = Apertura.Estado.EN_CURSO
            apertura.save(update_fields=['estado'])

        pasos = materializar_pasos_estacion(apertura=apertura, estacion=estacion, perfil=perfil)

    registrar_evento(
        usuario=token.creado_por, accion='apertura.estacion_enrolada', objeto=estacion,
        detalle={
            'apertura': apertura.pk, 'perfil': perfil.sufijo, 'token_prefijo': token.prefijo,
            'aprobada_automaticamente': True,
        },
    )
    logger.info(
        'Estación %s enrolada y aprobada automáticamente por la apertura #%s (perfil -%s)',
        codigo, apertura.pk, perfil.sufijo,
    )

    # Fuera de la transacción: publicar por MQTT no debe mantener abierta una transacción
    # de base, y un broker caído no tiene por qué deshacer el alta de la estación (los
    # pasos quedan en ERROR y se reintentan desde el panel).
    for paso in pasos:
        ejecutar_paso(paso)
    recalcular_estado_apertura(apertura)
    return estacion


def materializar_pasos_estacion(*, apertura, estacion, perfil):
    """Crea los `PasoApertura` que le tocan a esta estación según su rol."""
    plantilla = apertura.plantilla
    aplicables = (
        plantilla.pasos
        .exclude(tipo=TipoPaso.MANUAL)
        .filter(rol_estacion__in=['', perfil.rol])
        .order_by('orden')
    )
    creados = []
    for paso_plantilla in aplicables:
        paso, nuevo = PasoApertura.objects.get_or_create(
            apertura=apertura, paso_plantilla=paso_plantilla, estacion=estacion,
            defaults={
                'orden': paso_plantilla.orden,
                'nombre': paso_plantilla.nombre,
                'tipo': paso_plantilla.tipo,
            },
        )
        if nuevo:
            _registrar(paso, EventoApertura.Paso.ENROLADA, f'Estación {estacion.codigo} enrolada por token')
            creados.append(paso)
    return creados


# --------------------------------------------------------------------------------------
# Ejecución de pasos
# --------------------------------------------------------------------------------------

def ejecutar_paso(paso):
    """Despacha un paso al módulo que hace el trabajo real.

    Los pasos manuales no se ejecutan: los cierra una persona desde el panel. Un paso que
    falla queda en ERROR con el motivo en `detalle` — no se reintenta solo, para que el
    operador vea qué pasó antes de repetir algo sobre un equipo en un local que está por
    abrir.
    """
    if paso.tipo == TipoPaso.MANUAL:
        return paso

    plantilla = paso.paso_plantilla
    paso.estado = PasoApertura.Estado.EN_CURSO
    paso.fecha_inicio = timezone.now()
    paso.save(update_fields=['estado', 'fecha_inicio'])

    try:
        if paso.tipo == TipoPaso.SCRIPT:
            _ejecutar_paso_script(paso, plantilla)
        elif paso.tipo == TipoPaso.SOFTWARE:
            _ejecutar_paso_software(paso, plantilla)
        elif paso.tipo == TipoPaso.DESPLIEGUE_POS:
            _ejecutar_paso_despliegue(paso, plantilla)
        elif paso.tipo == TipoPaso.VERIFICACION:
            _ejecutar_paso_verificacion(paso, plantilla)
        elif paso.tipo == TipoPaso.ACTIVO_ITAM:
            _ejecutar_paso_activo(paso)
    except Exception as exc:  # noqa: BLE001 — un paso que revienta no debe tumbar la apertura entera
        logger.exception('Paso de apertura %s falló', paso.pk)
        marcar_paso(paso, PasoApertura.Estado.ERROR, f'{type(exc).__name__}: {exc}')
    return paso


def marcar_paso(paso, estado, detalle=''):
    paso.estado = estado
    paso.detalle = detalle
    if estado in PasoApertura.ESTADOS_TERMINALES:
        paso.fecha_fin = timezone.now()
    paso.save(update_fields=['estado', 'detalle', 'fecha_fin'])
    evento = {
        PasoApertura.Estado.COMPLETADO: EventoApertura.Paso.COMPLETADO,
        PasoApertura.Estado.ERROR: EventoApertura.Paso.ERROR,
        PasoApertura.Estado.OMITIDO: EventoApertura.Paso.OMITIDO,
    }.get(estado, EventoApertura.Paso.ENVIADO)
    _registrar(paso, evento, detalle)
    return paso


def _ejecutar_paso_script(paso, plantilla):
    from apps.scripts.models import EjecucionScript
    from apps.scripts.services import registrar_ejecucion_script

    ejecucion = registrar_ejecucion_script(
        script=plantilla.script,
        destino_tipo=EjecucionScript.DestinoTipo.ESTACIONES,
        estaciones=[paso.estacion],
        unidad_negocio=paso.apertura.farmacia.unidad_negocio,
        timeout_segundos=plantilla.timeout_segundos,
        usuario=paso.apertura.creado_por,
    )
    paso.ejecucion_script = ejecucion
    paso.save(update_fields=['ejecucion_script'])
    _registrar(paso, EventoApertura.Paso.ENVIADO, f'Ejecución de script #{ejecucion.pk}')


def _ejecutar_paso_software(paso, plantilla):
    from apps.software.models import DestinoTipo, SolicitudInstalacion, TipoAccionInstalacion
    from apps.software.services import publicar_solicitud

    solicitud = SolicitudInstalacion.objects.create(
        version_aplicacion=plantilla.version_aplicacion,
        accion=TipoAccionInstalacion.INSTALAR,
        unidad_negocio=paso.apertura.farmacia.unidad_negocio,
        destino_tipo=DestinoTipo.ESTACIONES,
        creado_por=paso.apertura.creado_por,
    )
    solicitud.estaciones.set([paso.estacion])
    resultado = publicar_solicitud(solicitud)
    paso.solicitud_instalacion = solicitud
    paso.save(update_fields=['solicitud_instalacion'])
    if not resultado.exitoso:
        marcar_paso(paso, PasoApertura.Estado.ERROR, 'No se pudo publicar la instalación (¿broker caído?)')
        return
    _registrar(paso, EventoApertura.Paso.ENVIADO, f'Solicitud de instalación #{solicitud.pk}')


def _ejecutar_paso_despliegue(paso, plantilla):
    from apps.despliegues.services import publicar_despliegue_a_estacion

    despliegue = plantilla.despliegue
    if despliegue.estado == despliegue.Estado.BORRADOR:
        marcar_paso(
            paso, PasoApertura.Estado.ERROR,
            f'El despliegue {despliegue.version} sigue en borrador: aprobalo antes de usarlo en una apertura.',
        )
        return
    enviado = publicar_despliegue_a_estacion(despliegue, paso.estacion)
    if not enviado:
        marcar_paso(paso, PasoApertura.Estado.ERROR, 'No se pudo publicar el despliegue (¿broker caído?)')
        return
    _registrar(paso, EventoApertura.Paso.ENVIADO, f'Despliegue {despliegue.version} enviado')


def _ejecutar_paso_activo(paso):
    from apps.activos.services import crear_activos_desde_estaciones

    resumen = crear_activos_desde_estaciones(
        usuario=paso.apertura.creado_por, estaciones=[paso.estacion], aplicar=True,
    )
    if resumen['creados'] or resumen['vinculados'] or resumen['ya_vinculadas']:
        marcar_paso(paso, PasoApertura.Estado.COMPLETADO, '; '.join(resumen['detalle']) or 'Ya estaba en ITAM')
        return
    # El caso real que trae acá es "sin número de serie": el agente todavía no reportó el
    # dato de BIOS. No es un error del sitio, es que falta un heartbeat con info — se
    # reintenta desde el panel cuando la estación haya reportado.
    marcar_paso(paso, PasoApertura.Estado.ERROR, '; '.join(resumen['detalle']) or 'Sin datos suficientes')


def _ejecutar_paso_verificacion(paso, plantilla):
    cumple, detalle = evaluar_verificacion(estacion=paso.estacion, tipo=plantilla.verificacion,
                                           parametro=plantilla.parametro)
    if cumple:
        marcar_paso(paso, PasoApertura.Estado.COMPLETADO, detalle)
    else:
        # Queda EN_CURSO, no en ERROR: la mayoría de estas verificaciones dependen de que
        # el agente reporte algo que todavía no reportó (BitLocker, Windows Update,
        # inventario). `sincronizar_apertura` las vuelve a evaluar cuando llegue el dato.
        paso.detalle = detalle
        paso.save(update_fields=['detalle'])


def evaluar_verificacion(*, estacion, tipo, parametro=''):
    """Devuelve `(cumple, detalle)` leyendo lo que la estación ya reportó.

    Ninguna de estas comprobaciones le pide nada nuevo al agente: todas leen campos que
    llena por su cuenta o que se derivan del catálogo. Si el dato todavía no llegó, la
    verificación no cumple y dice exactamente qué falta.
    """
    estacion.refresh_from_db()
    if tipo == TipoVerificacion.BITLOCKER:
        if estacion.bitlocker_habilitado is None:
            return False, 'Falta consultar la información del equipo (BitLocker sin reportar).'
        if estacion.bitlocker_habilitado:
            return True, f'BitLocker activo ({estacion.bitlocker_metodo_proteccion or "método no reportado"}).'
        return False, 'El disco no está cifrado.'

    if tipo == TipoVerificacion.SOFTWARE_PRESENTE:
        from apps.software.models import SoftwareInstaladoDetectado
        if estacion.software_instalado_ultima_verificacion is None:
            return False, 'Falta escanear el software instalado en esta estación.'
        encontrado = SoftwareInstaladoDetectado.objects.filter(
            estacion=estacion, nombre__icontains=parametro,
        ).first()
        if encontrado is not None:
            return True, f'Encontrado: {encontrado.nombre} {encontrado.version}'.strip()
        return False, f'No se encontró "{parametro}" en el inventario de la estación.'

    if tipo == TipoVerificacion.WINDOWS_UPDATE_ESCANEADO:
        if estacion.windows_update_ultima_verificacion is None:
            motivo = estacion.windows_update_ultimo_error or 'nunca se escaneó'
            return False, f'Windows Update sin escanear ({motivo}).'
        return True, f'{estacion.windows_update_pendientes} actualización(es) pendiente(s) al último escaneo.'

    if tipo == TipoVerificacion.VERSION_POS_OBJETIVO:
        objetivo = estacion.farmacia.grupo.version_objetivo
        if not objetivo:
            return False, f'El grupo {estacion.farmacia.grupo.codigo} no tiene versión objetivo definida.'
        if not estacion.version_pos:
            return False, 'La estación todavía no reportó la versión del POS.'
        if estacion.desactualizada:
            return False, f'POS en {estacion.version_pos}, el objetivo del grupo es {objetivo}.'
        return True, f'POS en la versión objetivo ({objetivo}).'

    if tipo == TipoVerificacion.NODO_POS_COHERENTE:
        if not estacion.pos_bdd:
            return False, 'La estación todavía no reportó a qué nodo apunta su POS.'
        if estacion.nodo_discrepante:
            return False, (
                f'El POS apunta a {estacion.pos_bdd} y su grupo es {estacion.farmacia.grupo.bdd_pos}.'
            )
        return True, f'El POS apunta a {estacion.pos_bdd}, coherente con su grupo.'

    return False, f'Verificación desconocida: {tipo}'


# --------------------------------------------------------------------------------------
# Sincronización y cierre
# --------------------------------------------------------------------------------------

def sincronizar_apertura(apertura):
    """Relee el estado de los módulos que ejecutan el trabajo y reevalúa las verificaciones.

    Existe porque los pasos no se enteran solos: el resultado de un script llega por MQTT a
    `apps.mqtt_worker`, que actualiza `ResultadoEjecucionScript` sin saber que esa ejecución
    pertenece a una apertura. En vez de acoplar el worker a este módulo, la apertura va y
    lee. La llama la vista de detalle del panel (mismo patrón de polling que despliegues) y
    el comando `sincronizar_aperturas`.
    """
    from apps.scripts.models import ResultadoEjecucionScript
    from apps.software.models import ResultadoInstalacion

    pendientes = apertura.pasos.exclude(estado__in=PasoApertura.ESTADOS_TERMINALES).select_related(
        'paso_plantilla', 'estacion',
    )
    for paso in pendientes:
        if paso.tipo == TipoPaso.SCRIPT and paso.ejecucion_script_id:
            resultado = ResultadoEjecucionScript.objects.filter(
                ejecucion_id=paso.ejecucion_script_id, estacion=paso.estacion,
            ).first()
            if resultado is None:
                continue
            if resultado.estado == ResultadoEjecucionScript.Estado.COMPLETADO:
                marcar_paso(paso, PasoApertura.Estado.COMPLETADO, (resultado.stdout or '')[:500])
            elif resultado.estado in (
                ResultadoEjecucionScript.Estado.ERROR, ResultadoEjecucionScript.Estado.TIMEOUT,
            ):
                marcar_paso(
                    paso, PasoApertura.Estado.ERROR,
                    (resultado.stderr or resultado.get_estado_display())[:500],
                )

        elif paso.tipo == TipoPaso.SOFTWARE and paso.solicitud_instalacion_id:
            resultado = ResultadoInstalacion.objects.filter(
                solicitud_id=paso.solicitud_instalacion_id, estacion=paso.estacion,
            ).first()
            if resultado is None:
                continue
            if resultado.estado == ResultadoInstalacion.Estado.INSTALADO:
                marcar_paso(paso, PasoApertura.Estado.COMPLETADO, 'Instalado')
            elif resultado.estado == ResultadoInstalacion.Estado.ERROR:
                marcar_paso(paso, PasoApertura.Estado.ERROR, (resultado.detalle_error or 'Error')[:500])

        elif paso.tipo == TipoPaso.DESPLIEGUE_POS and paso.paso_plantilla.despliegue_id:
            from apps.despliegues.models import ResultadoDespliegue
            resultado = ResultadoDespliegue.objects.filter(
                despliegue_id=paso.paso_plantilla.despliegue_id, estacion=paso.estacion,
            ).first()
            if resultado is None:
                continue
            if resultado.estado == ResultadoDespliegue.Estado.APLICADO:
                marcar_paso(paso, PasoApertura.Estado.COMPLETADO, f'POS en {resultado.version_nueva}')
            elif resultado.estado in (ResultadoDespliegue.Estado.ERROR, ResultadoDespliegue.Estado.ROLLBACK):
                marcar_paso(paso, PasoApertura.Estado.ERROR, (resultado.detalle_error or 'Error')[:500])

        elif paso.tipo == TipoPaso.VERIFICACION:
            _ejecutar_paso_verificacion(paso, paso.paso_plantilla)

        elif paso.tipo == TipoPaso.ACTIVO_ITAM and paso.estado == PasoApertura.Estado.ERROR:
            # Reintento barato: el caso típico es que faltaba el número de serie y ya llegó.
            _ejecutar_paso_activo(paso)

    return recalcular_estado_apertura(apertura)


def recalcular_estado_apertura(apertura):
    """Completa la apertura cuando ya no queda nada obligatorio por hacer.

    "Nada obligatorio" incluye las estaciones que la plantilla marca como obligatorias y
    todavía no se enrolaron: una farmacia cuyo servidor nunca apareció no está abierta,
    por más que las dos cajas que sí llegaron hayan terminado sus pasos.
    """
    if apertura.estado not in (Apertura.Estado.APROBADA, Apertura.Estado.EN_CURSO):
        return apertura

    sufijos_enrolados = {
        p.estacion.codigo.partition('-')[2]
        for p in apertura.pasos.select_related('estacion') if p.estacion_id
    }
    faltan_estaciones = [
        perfil.sufijo
        for perfil in apertura.plantilla.perfiles_estacion.filter(obligatoria=True)
        if perfil.sufijo not in sufijos_enrolados
    ]

    pasos_abiertos = apertura.pasos.filter(
        paso_plantilla__obligatorio=True,
    ).exclude(estado__in=[PasoApertura.Estado.COMPLETADO, PasoApertura.Estado.OMITIDO]).exists()

    if faltan_estaciones or pasos_abiertos:
        return apertura

    apertura.estado = Apertura.Estado.COMPLETADA
    apertura.fecha_completada = timezone.now()
    apertura.save(update_fields=['estado', 'fecha_completada'])

    # La fecha de apertura del local es un dato del negocio que hasta ahora se cargaba a
    # mano y quedaba vacío en la mayoría de las farmacias. Acá se sabe de verdad.
    farmacia = apertura.farmacia
    if farmacia.fecha_apertura is None:
        farmacia.fecha_apertura = timezone.localdate()
        farmacia.save(update_fields=['fecha_apertura'])

    registrar_evento(usuario=apertura.creado_por, accion='apertura.completar', objeto=apertura)
    logger.info('Apertura #%s de %s completada', apertura.pk, farmacia.codigo)
    return apertura


def completar_paso_manual(*, paso, usuario, detalle=''):
    """Cierra un paso manual (AD, 2FA, circuito del proveedor) desde el panel."""
    if paso.tipo != TipoPaso.MANUAL:
        raise ValueError('Solo los pasos manuales se cierran a mano.')
    marcar_paso(paso, PasoApertura.Estado.COMPLETADO, detalle or f'Confirmado por {usuario}')
    registrar_evento(
        usuario=usuario, accion='apertura.completar_paso_manual', objeto=paso.apertura,
        detalle={'paso': paso.nombre},
    )
    recalcular_estado_apertura(paso.apertura)
    return paso
