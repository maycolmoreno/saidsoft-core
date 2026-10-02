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

# El respaldo corre a las 02:00 (deploy/saidsoft-respaldo.timer). 26h = un día más dos
# horas de gracia: alcanza para que un arranque tardío tras un corte de energía corra su
# ejecución atrasada sin que el panel grite, y no tanto como para tapar un día perdido.
RESPALDO_UMBRAL_HORAS = 26
