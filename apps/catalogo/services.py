"""Comandos puntuales del panel hacia el agente de una estación (ej. reiniciar, ejecutar_script)."""
import hashlib
import hmac
import json
import logging

import paho.mqtt.publish as mqtt_publish
from django.conf import settings

logger = logging.getLogger(__name__)


def _topico_comando(estacion) -> str:
    return f'/saidsof/agente/{estacion.codigo}/comando/'


def firmar_payload(**campos) -> str:
    """HMAC-SHA256 de los valores de `campos` unidos con "|", en el orden en que se pasan.

    No se firma el JSON serializado (evita divergencias de canonicalización
    entre `json.dumps` en Python y `System.Text.Json` en el agente C#): se
    firma un string simple de campos en un orden fijo, que el agente debe
    reconstruir exactamente igual para verificar. Los kwargs preservan orden
    de inserción en Python 3.7+, así que el orden de las llamadas a
    `firmar_payload(...)` en este módulo ES el contrato con el agente.
    """
    mensaje = '|'.join(str(v) for v in campos.values())
    return hmac.new(settings.COMANDO_HMAC_SECRET.encode(), mensaje.encode(), hashlib.sha256).hexdigest()


def _publicar_comando(estacion, payload: dict) -> bool:
    mqtt_conf = settings.MQTT_CONFIG
    auth = None
    if mqtt_conf['USERNAME']:
        auth = {'username': mqtt_conf['USERNAME'], 'password': mqtt_conf['PASSWORD']}
    tls = None
    if mqtt_conf['USE_TLS']:
        tls = {'ca_certs': mqtt_conf['CA_CERT'] or None}

    try:
        mqtt_publish.single(
            _topico_comando(estacion),
            json.dumps(payload),
            hostname=mqtt_conf['HOST'],
            port=mqtt_conf['PORT'],
            auth=auth,
            tls=tls,
            client_id=mqtt_conf['CLIENT_ID_PANEL'],
            retain=False,
        )
        return True
    except Exception:
        logger.exception('No se pudo enviar el comando "%s" a %s', payload.get('comando'), estacion.codigo)
        return False


def enviar_comando(estacion, comando: str) -> bool:
    """Publica un comando puntual (ej. "reiniciar") al agente de la estación.

    No usa retain: si la estación está desconectada en el momento de enviarlo,
    el comando se pierde (correcto para "reiniciar ahora" — no queremos que un
    reinicio pendiente se dispare solo al reconectar más tarde). Devuelve True
    si se pudo publicar (no confirma que el agente lo haya recibido/aplicado).
    """
    firma = firmar_payload(comando=comando)
    return _publicar_comando(estacion, {'comando': comando, 'firma': firma})


def enviar_script(estacion, *, ejecucion_id: int, resultado_id: int, tipo_script: str,
                   contenido: str, timeout_segundos: int) -> bool:
    """Publica una orden de "ejecutar_script" al agente de la estación.

    El agente responde el progreso (recibido/ejecutando/completado/error/timeout)
    en el tópico `script_estado`, manejado por `apps.mqtt_worker.services.manejar_estado_script`.
    """
    firma = firmar_payload(
        comando='ejecutar_script', ejecucion_id=ejecucion_id, resultado_id=resultado_id,
        tipo_script=tipo_script, timeout_segundos=timeout_segundos, contenido=contenido,
    )
    return _publicar_comando(estacion, {
        'comando': 'ejecutar_script', 'ejecucion_id': ejecucion_id, 'resultado_id': resultado_id,
        'tipo_script': tipo_script, 'contenido': contenido, 'timeout_segundos': timeout_segundos,
        'firma': firma,
    })


def resolver_estaciones(destino_tipo, *, unidad_negocio, grupos=None, farmacias=None, estaciones=None):
    """Resuelve el queryset de Estacion aprobada para un destino_tipo/grupos/farmacias/estaciones,
    siempre acotado a `unidad_negocio` (el tenant del Despliegue/EjecucionScript que llama).

    Compartido por `Despliegue.resolver_estaciones_destino` y
    `EjecucionScript` (apps/scripts): ambos envían algo a un conjunto de
    estaciones definido de la misma forma (cadena completa / grupos /
    farmacias / estaciones puntuales), así que la resolución vive una sola vez aquí.

    `unidad_negocio` es obligatorio y se aplica en la base del queryset, no solo en la
    rama "farmacias"/"estaciones": un Grupo (canal de versión) puede estar compartido
    por farmacias de varias unidades de negocio (ver apps.cumplimiento), así que
    "grupos"/"cadena" NUNCA deben devolver estaciones de un tenant distinto al del
    despliegue/ejecución que las pidió, aunque el grupo en sí sea compartido.
    """
    from apps.catalogo.models import Estacion

    aprobada = Estacion.EstadoAprobacion.APROBADA
    base = Estacion.objects.filter(estado_aprobacion=aprobada, farmacia__unidad_negocio=unidad_negocio)

    if destino_tipo == 'cadena':
        return base
    if destino_tipo == 'grupos':
        return base.filter(farmacia__grupo__in=grupos or [])
    if destino_tipo == 'farmacias':
        return base.filter(farmacia__in=farmacias or [])
    ids_estaciones = estaciones if estaciones is not None else Estacion.objects.none()
    return base.filter(pk__in=ids_estaciones)


def validar_destino_unidad_negocio(unidad_negocio, *, farmacias=None, estaciones=None):
    """Rechaza farmacias/estaciones elegidas a mano que no pertenezcan a `unidad_negocio`.

    Pensado para llamarse desde `Form.clean()` (DespliegueForm, EjecutarScriptForm) con
    los querysets de `cleaned_data`, antes de guardar — ahí `farmacias`/`estaciones` son
    instancias reales, no hace falta ir a la base de datos de nuevo.

    No valida `grupos` a propósito: un Grupo puede ser compartido entre unidades de
    negocio (ver docstring de `resolver_estaciones`), así que un grupo "ajeno" no es un
    error de por sí — `resolver_estaciones` ya se encarga de que solo aporte las
    estaciones que sí son de esta unidad de negocio.
    """
    from django.core.exceptions import ValidationError

    ajenas = [f.codigo for f in (farmacias or []) if f.unidad_negocio_id != unidad_negocio.id]
    if ajenas:
        raise ValidationError(
            f'Estas farmacias no pertenecen a {unidad_negocio.codigo}: {", ".join(ajenas)}.'
        )
    ajenas_est = [e.codigo for e in (estaciones or []) if e.farmacia.unidad_negocio_id != unidad_negocio.id]
    if ajenas_est:
        raise ValidationError(
            f'Estas estaciones no pertenecen a {unidad_negocio.codigo}: {", ".join(ajenas_est)}.'
        )


def marcar_estaciones_offline(*, minutos: int = 5) -> int:
    """Pasa a OFFLINE las estaciones sin heartbeat reciente (más viejo que `minutos`).

    El agente pone la estación ONLINE en cada heartbeat, pero nada la pasa a OFFLINE
    cuando se apaga o pierde la red — esta función cierra ese hueco. La llaman tanto el
    comando manual (`marcar_estaciones_offline`) como la tarea periódica de Celery
    (`apps.catalogo.tasks.marcar_estaciones_offline_task`), para no duplicar la lógica.
    """
    from datetime import timedelta

    from django.utils import timezone

    from apps.catalogo.models import Estacion

    umbral = timezone.now() - timedelta(minutes=minutos)
    # Se traen los objetos (no un .update() directo) porque evaluar_reglas_sin_heartbeat
    # necesita la unidad_negocio de cada estación afectada, no solo el conteo.
    afectadas = list(
        Estacion.objects.select_related('farmacia__unidad_negocio').filter(
            estado_conexion=Estacion.EstadoConexion.ONLINE, ultimo_heartbeat__lt=umbral,
        )
    )
    if afectadas:
        Estacion.objects.filter(pk__in=[e.pk for e in afectadas]).update(
            estado_conexion=Estacion.EstadoConexion.OFFLINE,
        )
        from apps.monitoreo.models import EstadoDispositivo
        from apps.monitoreo.services import evaluar_reglas_sin_heartbeat, registrar_estado_dispositivo
        evaluar_reglas_sin_heartbeat(afectadas)
        for estacion in afectadas:
            registrar_estado_dispositivo(estacion, fuente=EstadoDispositivo.Fuente.MQTT, en_linea=False)
    return len(afectadas)


# --- Acceso remoto interactivo (MeshCentral) ---
# A diferencia de enviar_comando/enviar_script (arriba), esto NO viaja por el
# broker MQTT propio: es un canal completamente aparte (navegador -> servidor
# MeshCentral -> MeshAgent). Estas funciones solo construyen strings (un
# comando PowerShell y URLs); no hacen ninguna llamada de red.

def generar_comando_instalacion_meshcentral(estacion) -> str:
    """One-liner de PowerShell para instalar el agente de MeshCentral en `estacion`.

    Pensado para pegarse tal cual en el ejecutor ad-hoc de Scripts RMM
    (apps/scripts). Descarga el instalador silencioso (installflags=2 =
    solo servicio, sin UI) del device group configurado en MESHCENTRAL_CONFIG.
    """
    conf = settings.MESHCENTRAL_CONFIG
    url_instalador = (
        f"{conf['SERVER_URL']}/meshagents?id={conf['AGENT_ARCH_ID']}"
        f"&meshid={conf['MESH_ID']}&installflags={conf['INSTALL_FLAGS']}"
    )
    return (
        # MeshCentral usa un certificado autofirmado en el piloto — Invoke-WebRequest de
        # Windows PowerShell 5.1 corta la descarga con "error inesperado de envío" contra
        # este TLS, incluso forzando SecurityProtocol=Tls12 y saltando la validación del
        # certificado (probado así antes, no alcanzó — reproducido de nuevo instalando de
        # verdad en ML006-A y MC001-B, ver PLAN_MODERNIZACION.md §9/§10). curl.exe (nativo
        # desde Windows 10 1803+, usa su propio stack TLS vía Schannel, no el de .NET
        # Framework que usa PowerShell 5.1) sí la baja bien con -k (salta verificación del
        # certificado autofirmado, equivalente al ServerCertificateValidationCallback de
        # antes).
        '$ruta = Join-Path $env:TEMP "meshagent.exe"; '
        f'curl.exe -k -sS -o $ruta "{url_instalador}"; '
        'if (-not (Test-Path $ruta)) { throw "No se pudo descargar el agente MeshCentral (curl.exe)." }; '
        'Start-Process -FilePath $ruta -Wait; '
        'Remove-Item $ruta -Force -ErrorAction SilentlyContinue'
    )


def _url_dispositivo_meshcentral(estacion, viewmode: str) -> str | None:
    if not estacion.meshcentral_node_id:
        return None
    conf = settings.MESHCENTRAL_CONFIG
    # Verificado contra una instancia real (6-ago-2026): MeshCentral v1.2.4 arma sus
    # propios links de escritorio/terminal como /login?viewmode=X&gotonode=Y — no
    # /index.html?node=Y&viewmode=X (formato viejo/de otra versión, daba 404 en la
    # práctica). "/login" funciona igual estando ya autenticado: redirige directo al
    # dispositivo.
    return f"{conf['SERVER_URL']}/login?viewmode={viewmode}&gotonode={estacion.meshcentral_node_id}"


def url_escritorio_remoto_meshcentral(estacion) -> str | None:
    """URL de MeshCentral para ver el escritorio remoto de `estacion`, o None si
    todavía no tiene un node_id vinculado."""
    return _url_dispositivo_meshcentral(estacion, settings.MESHCENTRAL_CONFIG['VIEWMODE_ESCRITORIO'])


def url_terminal_remoto_meshcentral(estacion) -> str | None:
    """URL de MeshCentral para abrir una terminal remota en `estacion`, o None si
    todavía no tiene un node_id vinculado."""
    return _url_dispositivo_meshcentral(estacion, settings.MESHCENTRAL_CONFIG['VIEWMODE_TERMINAL'])


# --- Clave de recuperación de BitLocker ---
# Igual que MeshCentral: función puntual, no un canal nuevo. La clave llega cifrada
# desde el agente (ver apps.mqtt_worker.services.manejar_info_equipo) y solo se
# descifra aquí, bajo demanda, para la vista que la muestra (permiso propio + auditada).

def obtener_clave_bitlocker_descifrada(estacion) -> str | None:
    """Clave de recuperación de `estacion` en texto plano, o None si no hay ninguna
    registrada (nunca se reportó, o el equipo no usa BitLocker)."""
    from apps.catalogo import crypto
    from apps.catalogo.models import ClaveRecuperacionBitLocker

    try:
        clave = estacion.clave_bitlocker
    except ClaveRecuperacionBitLocker.DoesNotExist:
        return None
    return crypto.descifrar(clave.clave_cifrada)


def url_grabaciones_meshcentral(estacion) -> str | None:
    """URL de MeshCentral para revisar las grabaciones de sesión de `estacion`, o None
    si todavía no tiene un node_id vinculado.

    A diferencia de escritorio/terminal (que sí tienen un `viewmode` fijo y verificado
    con un servidor de prueba), la lista de grabaciones no tiene un deep-link estable
    por dispositivo — abre la ficha general del equipo y desde ahí hay que entrar a la
    pestaña "Recordings" a mano. Requiere además que la grabación esté habilitada para
    el device group en el `config.json` del servidor MeshCentral (ver
    deploy/README-produccion.md) — sin eso, aunque el link abra, no habrá nada grabado.
    """
    if not estacion.meshcentral_node_id:
        return None
    conf = settings.MESHCENTRAL_CONFIG
    return f"{conf['SERVER_URL']}/login?gotonode={estacion.meshcentral_node_id}"


def calcular_matriz_cumplimiento(unidades_negocio, *, umbral_online_minutos=5):
    """Grupos con cobertura de estaciones/farmacias dentro de `unidades_negocio`, con
    conteos de conformidad de versión, en línea y pendientes de aprobación.

    Reusada por el dashboard (`apps.panel.views.dashboard`, con las unidades visibles
    del usuario) y por el reporte por cliente (`apps.panel.views.reportes`, con una
    sola unidad) — misma agregación, distinto alcance.
    """
    from datetime import timedelta

    from django.db.models import Count, F, Q
    from django.utils import timezone

    from apps.catalogo.models import Estacion, Grupo

    umbral_online = timezone.now() - timedelta(minutes=umbral_online_minutos)
    en_alcance = Q(farmacias__unidad_negocio__in=unidades_negocio)

    grupos = Grupo.objects.filter(en_alcance).distinct().annotate(
        total_estaciones=Count('farmacias__estaciones', filter=en_alcance, distinct=True),
        total_farmacias=Count('farmacias', filter=en_alcance, distinct=True),
        conformes=Count(
            'farmacias__estaciones',
            filter=Q(farmacias__estaciones__version_pos=F('version_objetivo')) & en_alcance,
            distinct=True,
        ),
        online=Count(
            'farmacias__estaciones',
            filter=Q(farmacias__estaciones__ultimo_heartbeat__gte=umbral_online) & en_alcance,
            distinct=True,
        ),
        pendientes=Count(
            'farmacias__estaciones',
            filter=Q(farmacias__estaciones__estado_aprobacion=Estacion.EstadoAprobacion.PENDIENTE) & en_alcance,
            distinct=True,
        ),
    ).order_by('codigo')

    for g in grupos:
        g.pct_conforme = round(100 * g.conformes / g.total_estaciones) if g.total_estaciones else None
    return grupos


# --- Alta masiva de farmacias desde un CSV de inventario de red ---------------------
#
# Reusada por el comando `importar_farmacias` (SSH/docker exec) y por la vista de
# importación del admin (`FarmaciaAdmin.importar_view`, /admin/catalogo/farmacia/
# importar/) — misma lógica, dos formas de disparar el mismo import.

PREFIJOS_UNIDAD_NEGOCIO_FARMACIA = {
    'M': 'MIA',
    'G': 'SG',
}

_NEGATIVOS_BACKUP = {'', 'no', 'inactivo', 'false', '0', 'n'}


def _unidad_negocio_por_prefijo(codigo):
    if codigo[:1].isdigit():
        return '7DIAS'
    return PREFIJOS_UNIDAD_NEGOCIO_FARMACIA.get(codigo[:1])


def _normalizar_encabezado(texto):
    import unicodedata
    sin_acentos = unicodedata.normalize('NFKD', texto).encode('ascii', 'ignore').decode('ascii')
    return sin_acentos.strip().lower()


def _encontrar_columna(fieldnames, *pistas):
    for nombre in fieldnames:
        normalizado = _normalizar_encabezado(nombre)
        if any(pista in normalizado for pista in pistas):
            return nombre
    return None


def _fila_tiene_backup(valor):
    return _normalizar_encabezado(valor) not in _NEGATIVOS_BACKUP


class ResultadoImportacionFarmacias:
    """Resultado de `importar_farmacias_desde_csv` — mismo shape para el comando de
    management y para la vista de admin, cada uno lo presenta a su manera."""

    def __init__(self):
        self.columnas = {}
        self.creadas = []
        self.actualizadas = []
        self.omitidas = []
        self.errores = []
        self.grupos_nuevos = []


def importar_farmacias_desde_csv(archivo_texto, *, dry_run=False, actualizar=False):
    """Crea/actualiza `Farmacia` desde un CSV de inventario de red (ver docstring de
    `apps.catalogo.management.commands.importar_farmacias` para el detalle completo del
    mapeo de columnas y de la deducción de unidad de negocio por prefijo de código).

    `archivo_texto` es cualquier iterable de líneas de texto (un archivo abierto en
    modo texto, o un `io.StringIO`) — quien llama decide de dónde viene (disco o un
    archivo subido por HTTP) y cómo lo decodifica.
    """
    import csv

    from django.db import transaction

    from apps.catalogo.models import Farmacia, Grupo, UnidadNegocio

    resultado = ResultadoImportacionFarmacias()

    lector = csv.DictReader(archivo_texto)
    if not lector.fieldnames:
        raise ValueError('El CSV no tiene encabezados.')

    col_codigo = _encontrar_columna(lector.fieldnames, 'id', 'codigo', 'código')
    col_ciudad = _encontrar_columna(lector.fieldnames, 'ciudad', 'ubicacion', 'ubicación')
    col_provincia = _encontrar_columna(lector.fieldnames, 'provincia')
    col_nodo = _encontrar_columna(lector.fieldnames, 'nodo', 'grupo')
    col_segmento = _encontrar_columna(lector.fieldnames, 'segmento')
    col_tipo_enlace = _encontrar_columna(lector.fieldnames, 'enlace')
    col_backup = _encontrar_columna(lector.fieldnames, 'backup')
    if not col_codigo or not col_nodo:
        raise ValueError(
            f'No se detectaron las columnas necesarias en {lector.fieldnames!r}. '
            'Hace falta al menos una columna de código (id/código) y una de nodo/grupo.',
        )
    resultado.columnas = {
        'código': col_codigo, 'ciudad': col_ciudad, 'provincia': col_provincia, 'nodo': col_nodo,
        'segmento': col_segmento, 'tipo_enlace': col_tipo_enlace, 'backup': col_backup,
    }

    grupos_cache = {g.codigo: g for g in Grupo.objects.all()}
    unidades_cache = {u.codigo: u for u in UnidadNegocio.objects.all()}

    with transaction.atomic():
        sp = transaction.savepoint()
        for fila_num, fila in enumerate(lector, start=2):
            codigo = (fila.get(col_codigo) or '').strip().upper()
            if not codigo:
                continue
            ciudad = (fila.get(col_ciudad) or '').strip() if col_ciudad else ''
            provincia = (fila.get(col_provincia) or '').strip() if col_provincia else ''
            ubicacion = f'{ciudad}, {provincia}' if ciudad and provincia else (ciudad or provincia)
            segmento_red = (fila.get(col_segmento) or '').strip() if col_segmento else ''
            tipo_enlace = (fila.get(col_tipo_enlace) or '').strip() if col_tipo_enlace else ''
            tiene_backup = _fila_tiene_backup(fila.get(col_backup) or '') if col_backup else False
            nodo = (fila.get(col_nodo) or '').strip().upper()

            if not nodo:
                resultado.errores.append(f'fila {fila_num} ({codigo}): sin valor de nodo/grupo.')
                continue

            # Validar largo ANTES de tocar la base — un valor que no entra en la
            # columna (ej. un NODO más largo que Grupo.codigo, max_length=10) tiraba
            # un DataError sin manejar en pleno grupo.save()/Farmacia.save(), que
            # devolvía un 500 crudo en vez de reportarse como error de esta fila
            # (encontrado en producción, 12-ago-2026, con un NODO real más largo de
            # lo esperado).
            campos_con_largo_maximo = {
                'código': (codigo, Farmacia._meta.get_field('codigo').max_length),
                'nodo/grupo': (nodo, Grupo._meta.get_field('codigo').max_length),
                'ubicación': (ubicacion, Farmacia._meta.get_field('ubicacion').max_length),
                'segmento de red': (segmento_red, Farmacia._meta.get_field('segmento_red').max_length),
                'tipo de enlace': (tipo_enlace, Farmacia._meta.get_field('tipo_enlace').max_length),
            }
            demasiado_largos = [
                f'{etiqueta} "{valor}" tiene {len(valor)} caracteres (máximo {maximo})'
                for etiqueta, (valor, maximo) in campos_con_largo_maximo.items()
                if len(valor) > maximo
            ]
            if demasiado_largos:
                resultado.errores.append(f'fila {fila_num} ({codigo}): ' + '; '.join(demasiado_largos))
                continue

            codigo_unidad = _unidad_negocio_por_prefijo(codigo)
            if not codigo_unidad:
                resultado.errores.append(
                    f'fila {fila_num} ({codigo}): prefijo de código sin mapeo a unidad de '
                    'negocio conocido (M->MIA, G->SG, dígito->7DIAS).',
                )
                continue
            unidad = unidades_cache.get(codigo_unidad)
            if unidad is None:
                resultado.errores.append(
                    f'fila {fila_num} ({codigo}): la unidad de negocio {codigo_unidad} no existe '
                    'todavía en SAIDSOFT — hay que crearla primero (Admin > Catálogo > Unidades '
                    'de negocio) antes de poder importar esta fila.',
                )
                continue

            grupo = grupos_cache.get(nodo)
            if grupo is None:
                grupo = Grupo(codigo=nodo)
                if not dry_run:
                    grupo.save()
                grupos_cache[nodo] = grupo
                resultado.grupos_nuevos.append(nodo)

            existente = Farmacia.objects.filter(codigo=codigo).first()
            if existente:
                if actualizar:
                    existente.ubicacion = ubicacion
                    existente.grupo = grupo
                    existente.unidad_negocio = unidad
                    existente.segmento_red = segmento_red
                    existente.tipo_enlace = tipo_enlace
                    existente.tiene_backup = tiene_backup
                    if not dry_run:
                        existente.save(update_fields=[
                            'ubicacion', 'grupo', 'unidad_negocio',
                            'segmento_red', 'tipo_enlace', 'tiene_backup',
                        ])
                    resultado.actualizadas.append(codigo)
                else:
                    resultado.omitidas.append(codigo)
                continue

            if not dry_run:
                Farmacia.objects.create(
                    codigo=codigo, ubicacion=ubicacion, grupo=grupo, unidad_negocio=unidad,
                    segmento_red=segmento_red, tipo_enlace=tipo_enlace, tiene_backup=tiene_backup,
                )
            resultado.creadas.append(codigo)

        if dry_run:
            transaction.savepoint_rollback(sp)
        else:
            transaction.savepoint_commit(sp)

    return resultado
