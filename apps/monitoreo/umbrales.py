"""Umbrales de salud de la propia plataforma, compartidos por la consola y el panel.

Viven acá y no en `apps.panel.views.dashboard`, que es de donde salían hasta el
2-oct-2026. El problema no era que estuvieran duplicados —no lo estaban, el comando los
importaba— sino de qué lado quedaba la fuente de verdad: una vista. Eso obligaba a
`apps.monitoreo.management.commands.verificar_salud` a importar de `apps.panel`, o sea
al dominio a depender de la presentación, y cerraba un ciclo (`panel` importa 23
símbolos de `monitoreo`). Un umbral operativo no es un detalle de cómo se pinta una
pantalla; que estuviera en la vista era el accidente.

Mismo criterio que `apps.panel.umbrales` para los colores de CPU/RAM/disco: un valor que
necesitan dos consumidores no es de ninguno de los dos.

Acá van solo los COMPARTIDOS. `BEAT_UMBRAL_MINUTOS` y `DISCO_LIBRE_MINIMO_PCT` siguen en
`verificar_salud` a propósito: hoy los usa únicamente ese comando, y una constante con un
solo consumidor vive con su consumidor. El día que el panel los muestre, se mudan acá.
"""

# 3x el intervalo de latido del worker (30s) con margen para jitter/latencia de red,
# antes de considerar que dejó de reportarse.
WORKER_MQTT_UMBRAL_SEGUNDOS = 90

# --- Cuándo una estación cuenta como "en línea" ------------------------------------
#
# DOS umbrales, con nombre propio y motivo distinto. Hasta el 10-oct-2026 había TRES
# criterios escritos en tres lugares, con dos valores, y ninguno era dueño del concepto:
#
#   - `apps.panel.views.dashboard.ONLINE_UMBRAL_MINUTOS = 5`  -> el tablero
#   - `TOLERANCIA_FRESCURA_MINUTOS['estaciones'] = 10`        -> el Centro y el bot
#   - `marcar_estaciones_offline(minutos=5)`                  -> el campo estado_conexion
#
# El síntoma era que una estación con el último latido hace 7 minutos estaba, en el mismo
# instante, CAÍDA en el tablero, EN LÍNEA en el Centro y CAÍDA en el listado. Los tres
# números eran defendibles por separado y se contradecían juntos (hallazgo D-01 de
# docs/auditoria/centro-monitoreo/).
#
# No se colapsan en uno porque responden preguntas distintas, y colapsarlos obligaría a
# elegir entre dos males: con 5 min para todo, una estación sana entra y sale de la lista
# cada vez que la red hace un hipo; con 10 min para todo, la alerta `sin_heartbeat` se
# abriría CINCO MINUTOS MÁS TARDE que hoy, que es retroceder en lo único que de verdad
# despierta a alguien.
#
# Decisión del usuario, 10-oct-2026. La regla para no volver a tres: **el umbral de
# VISUALIZACIÓN es el único que decide lo que se cuenta en una pantalla; el de ALERTA es
# el único que decide cuándo se abre una alerta.** Si una pantalla necesita el segundo, es
# porque está mostrando el estado de las alertas, no el de la flota.

# Lo que CUALQUIER pantalla usa para decir "en línea". Sale del latido del agente (~30 s)
# más el margen de la tarea que marca offline (60 s): 10 minutos son 20 latidos perdidos,
# que ya no es jitter. Es el mismo valor que el Centro venía usando bien.
ONLINE_UMBRAL_MINUTOS = 10

# Lo que decide `estado_conexion` y la apertura de la alerta `sin_heartbeat`. Más estricto
# a propósito: acá el valor ES la velocidad de detección, y una alerta que llega tarde no
# sirve. No se usa para pintar pantallas.
ALERTA_SIN_LATIDO_UMBRAL_MINUTOS = 5

# El respaldo corre a las 02:00 (deploy/saidsoft-respaldo.timer). 26h = un día más dos
# horas de gracia: alcanza para que un arranque tardío tras un corte de energía corra su
# ejecución atrasada sin que el panel grite, y no tanto como para tapar un día perdido.
RESPALDO_UMBRAL_HORAS = 26


# --- Consumo del enlace contra el ancho CONTRATADO ---------------------------------
#
# Los mismos para el color del panel (`apps.panel.umbrales`, que los reexporta) y para el
# aviso de saturación (`apps.monitoreo.saturacion`): si el panel pintara rojo a un número y
# el aviso saliera a otro, alguien preguntaría con razón cuál de los dos miente. El aviso
# abre al CRITICAL sostenido y cierra al bajar del WARNING (histéresis).
BW_CONTRATADO_UMBRAL_WARNING_PCT = 70
BW_CONTRATADO_UMBRAL_CRITICAL_PCT = 90
