"""Estado de la propia plataforma de monitoreo, en una sola consulta.

Existe porque hasta el 10-oct-2026 la barra superior del panel afirmaba
**"Todos los sistemas operativos"** con un LED verde fijo, en todas las pantallas, sin
comprobar nada — incluso mientras el aviso rojo "Worker MQTT sin señal" estaba tres
centímetros más abajo en la misma pantalla, contradiciéndola
(ver docs/auditoria/centro-monitoreo/, hallazgo D-06).

Eso contradecía una regla que el proyecto ya había decidido y escrito tres veces:

- `fuente_desactualizada()` trata `None` como viejo, "porque un tablero en verde con
  datos viejos es peor que no tener tablero".
- `apps.panel.umbrales.clasificar()` devuelve `sin_dato` y no `ok`, porque "un recurso
  que no se pudo medir no es un recurso sano".
- `panel/_estado_chip.html` tiene un estado "sin dato" cuyo title dice literalmente
  "No hay medición: no es lo mismo que estar bien".

La barra era la única excepción, y era la que se veía siempre.

**Vive en `monitoreo` y no en `apps.panel` a propósito.** Es el mismo criterio que movió
los umbrales a `apps.monitoreo.umbrales` el 2-oct-2026: el dominio no puede depender de la
presentación. Acá el motivo es más fuerte todavía, porque hay tres consumidores y ninguno
es dueño del dato — la barra superior (todas las pantallas), la Zona 0 del tablero, y el
comando `verificar_salud` el día que se unifique (ver abajo).

Qué NO hace, y por qué importa:

- **No afirma que el worker esté procesando mensajes.** `WorkerHeartbeat` solo prueba que
  el proceso vive: lo escribe un hilo aparte (`run_mqtt_worker._latido_periodico`) sin
  consultar `client.is_connected()`. Un worker desconectado del broker se ve sano. Por eso
  el componente del broker se reporta `sin_dato` ("sin verificar") en vez de en verde: un
  componente sin mecanismo de comprobación no se pinta de verde, se dice que falta
  construirlo (hallazgo D-05/D-12, Fase D del plan).
- **No consulta el estado de las farmacias.** Esto es salud de la plataforma, no de la
  flota. No tiene tenant: un worker caído lo está para todos los clientes.
- **No cachea.** Es UNA consulta a una tabla de tres filas. Cachearla metería retraso justo
  en el indicador cuyo único valor es decir la verdad ahora, y a cambio de nada medible.

Pendiente declarado (Fase D): `verificar_salud` sigue teniendo su propia copia de estas
comparaciones en `_revisar_latidos`. No se unificó acá para no cambiar el contrato de
salida de un comando que un temporizador de systemd ya consume, en una fase cuyo criterio
de aceptación es que ningún número cambie. Los DOS lados leen los umbrales de
`apps.monitoreo.umbrales`, que es lo que impide que discrepen sobre qué es "sano".
"""
from datetime import datetime
from typing import NamedTuple

from django.utils import timezone

from apps.monitoreo.umbrales import RESPALDO_UMBRAL_HORAS, WORKER_MQTT_UMBRAL_SEGUNDOS

# El nombre con el que cada proceso registra su latido. `NOMBRE_WORKER_MQTT` y
# `NOMBRE_RESPALDO` se importan de donde se escriben, para que renombrar uno rompa acá y
# no en silencio. 'meshcentral_worker' se declara igual que en `verificar_salud`.
NOMBRE_WORKER_MESHCENTRAL = 'meshcentral_worker'

# Nombre de fila de Celery Beat. Vive ACÁ y no junto a la tarea que la escribe
# (`apps.monitoreo.tasks.latido_beat_task`) por una razón de peso, literal: este módulo lo
# importa un context processor que corre en TODAS las páginas del panel, y `tasks.py`
# arrastra `mikrotik` y con él pysnmp. Importarlo desde acá metía una dependencia de SNMP
# en el camino de cada petición web para leer una cadena de once caracteres. La tarea lo
# importa de este módulo, que no importa nada más que `umbrales`.
NOMBRE_BEAT = 'celery_beat'


class ComponenteSalud(NamedTuple):
    """Un componente de la plataforma y lo que hoy se puede afirmar de él.

    `estado` usa el vocabulario de `panel/_estado_chip.html` —ok / warning / critical /
    sin_dato— para que la plantilla pueda pintarlo con el distintivo que ya existe, con
    color, forma Y palabra. `sin_dato` significa "no hay mecanismo para comprobarlo", que
    no es lo mismo que estar bien.

    `verificable` separa "lo medí y está mal" de "no lo puedo medir". El agregado de la
    barra superior solo cuenta los verificables: si contara los demás, la barra estaría
    permanentemente en rojo por algo que nadie va a arreglar hoy, y un indicador
    permanentemente en rojo informa tan poco como uno permanentemente en verde.
    """

    clave: str
    etiqueta: str
    estado: str
    detalle: str
    instante: datetime | None = None
    verificable: bool = True

    @property
    def ok(self) -> bool:
        """Comprobado y bien. `warning` no es `ok`: algo se comprobó y no está del todo."""
        return self.estado == 'ok'

    @property
    def critico(self) -> bool:
        """Comprobado y mal. Es lo que justifica un aviso rojo o despertar a alguien.

        Existe separado de `not ok` por un caso que ya mordió: el aviso "Worker MQTT sin
        señal" del tablero pasó a dispararse con un worker que SÍ estaba latiendo, solo
        porque todavía no reportaba su conexión al broker (`conectado=None`, un worker
        anterior a ese campo). El aviso afirma que no se están procesando despliegues ni
        latidos, y eso con un worker vivo es falso.

        La regla: `critico` para lo que exige acción, `ok` para lo confirmado sano, y el
        ámbar de en medio se muestra sin gritar.
        """
        return self.estado == 'critical'


def _detalle_latido(atraso_segundos: float, umbral_segundos: int) -> str:
    if atraso_segundos < 90:
        return 'último latido hace %.0f s (umbral %d s)' % (atraso_segundos, umbral_segundos)
    minutos = atraso_segundos / 60
    if minutos < 90:
        return 'último latido hace %.0f min (umbral %d s)' % (minutos, umbral_segundos)
    return 'último latido hace %.0f h (umbral %d s)' % (minutos / 60, umbral_segundos)


def estado_latidos() -> list[ComponenteSalud]:
    """Los componentes que dejan latido, en **una sola consulta**.

    Una consulta y no tres porque esto lo consume un context processor que corre en TODAS
    las páginas del panel, y el presupuesto es una. Se traen las tres filas de golpe y se
    comparan en Python — es el mismo patrón que ya usa `verificar_salud._revisar_latidos`.
    """
    from apps.mqtt_worker.management.commands.registrar_latido import NOMBRE_RESPALDO
    from apps.mqtt_worker.models import WorkerHeartbeat
    from apps.mqtt_worker.services import NOMBRE_WORKER_MQTT

    ahora = timezone.now()
    latidos = {h.nombre: h for h in WorkerHeartbeat.objects.all()}
    componentes = []

    for clave, etiqueta in (
        (NOMBRE_WORKER_MQTT, 'Worker MQTT'),
        (NOMBRE_WORKER_MESHCENTRAL, 'Worker MeshCentral'),
    ):
        fila = latidos.get(clave)
        if fila is None:
            componentes.append(ComponenteSalud(
                clave=clave, etiqueta=etiqueta, estado='critical',
                detalle='nunca registró un latido',
            ))
            continue
        atraso = (ahora - fila.ultimo_latido).total_seconds()
        if atraso > WORKER_MQTT_UMBRAL_SEGUNDOS:
            # El proceso dejó de latir. Lo demás da igual: sin proceso no hay nada.
            componentes.append(ComponenteSalud(
                clave=clave, etiqueta=etiqueta, estado='critical',
                detalle=_detalle_latido(atraso, WORKER_MQTT_UMBRAL_SEGUNDOS),
                instante=fila.ultimo_latido,
            ))
            continue

        # El proceso late. Ahora las dos preguntas que el latido no respondía:
        #
        #   conectado=False -> el proceso vive y NO está hablando con el broker. Es el
        #     estado que antes se veía en verde: `loop_forever()` reintenta en silencio
        #     mientras el hilo de latido sigue escribiendo.
        #   conectado=None  -> ese worker no sabe responder (versión anterior al campo).
        #     "Sin verificar", no "mal": afirmarlo roto sería inventar.
        if fila.conectado is False:
            componentes.append(ComponenteSalud(
                clave=clave, etiqueta=etiqueta, estado='critical',
                detalle='el proceso vive pero NO está conectado al broker',
                instante=fila.ultimo_latido,
            ))
            continue
        detalle = _detalle_latido(atraso, WORKER_MQTT_UMBRAL_SEGUNDOS)
        if fila.conectado is None:
            componentes.append(ComponenteSalud(
                clave=clave, etiqueta=etiqueta, estado='warning',
                detalle='%s · conexión al broker sin verificar (¿worker viejo?)' % detalle,
                instante=fila.ultimo_latido,
            ))
            continue
        componentes.append(ComponenteSalud(
            clave=clave, etiqueta=etiqueta, estado='ok',
            detalle='%s · conectado · %s mensajes' % (detalle, fila.mensajes_procesados),
            instante=fila.ultimo_latido,
        ))

    # El BROKER, deducido de la conexión real del worker y no de su API de administración.
    #
    # La auditoría proponía consultar EMQX con `apps.mqtt_worker.emqx_admin` desde una
    # tarea de fondo. No hace falta y además diría menos: lo que importa operativamente no
    # es si el panel de administración de EMQX contesta, sino si el broker **acepta al
    # worker**, y eso es exactamente lo que `is_connected()` reporta en cada latido. Sale
    # gratis (ya está en la fila), no necesita credenciales de administración en el camino
    # del render, y no puede quedar en verde mientras el canal real está roto.
    #
    # Mientras ningún worker sepa responder, el componente se declara "sin verificar" en
    # gris en vez de omitirse: que falte la comprobación es información útil.
    mqtt = latidos.get(NOMBRE_WORKER_MQTT)
    if mqtt is None or mqtt.conectado is None:
        componentes.append(ComponenteSalud(
            clave='broker_mqtt', etiqueta='Broker MQTT', estado='sin_dato',
            detalle='sin verificar: ningún worker reportó su conexión todavía',
            verificable=False,
        ))
    elif mqtt.conectado:
        componentes.append(ComponenteSalud(
            clave='broker_mqtt', etiqueta='Broker MQTT', estado='ok',
            detalle='acepta la conexión del worker MQTT',
            instante=mqtt.ultimo_latido,
        ))
    else:
        componentes.append(ComponenteSalud(
            clave='broker_mqtt', etiqueta='Broker MQTT', estado='critical',
            detalle='el worker no logra conectarse: nada de la flota está llegando',
            instante=mqtt.ultimo_latido,
        ))

    # Celery Beat. Desde el 10-oct-2026 deja latido propio (`latido_beat_task`), así que
    # esto ya no se infiere de que una tabla de muestras siga creciendo. El umbral es el
    # mismo de los workers: la tarea corre cada minuto, así que 90 s es un ciclo perdido.
    #
    # La frescura de `MuestraRedFarmacia` NO desaparece del tablero: sigue mostrándose
    # como "frescura de los datos", que es otra pregunta —si el sondeo está produciendo—
    # y ahora se puede leer sin tener que usarla como proxy de dos cosas a la vez.
    beat = latidos.get(NOMBRE_BEAT)
    if beat is None:
        componentes.append(ComponenteSalud(
            clave=NOMBRE_BEAT, etiqueta='Tareas periódicas', estado='sin_dato',
            detalle='sin verificar: Beat todavía no registró ningún latido',
            verificable=False,
        ))
    else:
        atraso_beat = (ahora - beat.ultimo_latido).total_seconds()
        componentes.append(ComponenteSalud(
            clave=NOMBRE_BEAT, etiqueta='Tareas periódicas',
            estado='ok' if atraso_beat <= WORKER_MQTT_UMBRAL_SEGUNDOS else 'critical',
            detalle=_detalle_latido(atraso_beat, WORKER_MQTT_UMBRAL_SEGUNDOS),
            instante=beat.ultimo_latido,
        ))

    # El respaldo es el único que distingue "nunca corrió" de "atrasado", y no por
    # simetría: una instalación nueva no tiene respaldo y eso no es una falla. Pero
    # tampoco es "al día" -- se reporta como advertencia, no como crítico ni como ok.
    # (`verificar_salud` lo cuenta como OK a propósito, para no llenar de ruido el primer
    # día de un despliegue; acá es una pantalla, no un código de salida, y el matiz cabe.)
    respaldo = latidos.get(NOMBRE_RESPALDO)
    if respaldo is None:
        componentes.append(ComponenteSalud(
            clave='respaldo', etiqueta='Respaldo', estado='warning',
            detalle='sin registro todavía (¿instalación nueva?)',
        ))
    else:
        horas = (ahora - respaldo.ultimo_latido).total_seconds() / 3600
        componentes.append(ComponenteSalud(
            clave='respaldo', etiqueta='Respaldo',
            estado='ok' if horas <= RESPALDO_UMBRAL_HORAS else 'critical',
            detalle='último hace %.0f h (umbral %d h)' % (horas, RESPALDO_UMBRAL_HORAS),
            instante=respaldo.ultimo_latido,
        ))

    return componentes


def resumen_salud(componentes: list[ComponenteSalud]) -> dict:
    """Lo que la barra superior necesita para no mentir.

    `estado` es el PEOR de los componentes verificables, que es la única agregación
    honesta: con un worker caído, "4 de 5 bien" es cierto y es irrelevante.

    `texto` nombra su propio alcance ("Plataforma"). Importa porque esta barra se ve en
    todas las pantallas y antes decía "Todos los sistemas operativos", que afirmaba algo
    sobre TODO -- incluida la flota, que esta función no mira. Un indicador que dice de
    qué habla se puede creer; el anterior no se podía ni comprobar.
    """
    verificables = [c for c in componentes if c.verificable]
    sin_verificar = [c for c in componentes if not c.verificable]
    problemas = [c for c in verificables if c.estado == 'critical']
    avisos = [c for c in verificables if c.estado == 'warning']

    if problemas:
        estado = 'critical'
        texto = 'Plataforma degradada'
    elif avisos:
        estado = 'warning'
        texto = 'Plataforma con avisos'
    else:
        estado = 'ok'
        texto = 'Plataforma verificada'

    return {
        'estado': estado,
        'texto': texto,
        'componentes': componentes,
        'afectados': problemas + avisos,
        'total_verificables': len(verificables),
        'ok': len([c for c in verificables if c.estado == 'ok']),
        'sin_verificar': len(sin_verificar),
    }
