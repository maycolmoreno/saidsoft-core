"""Utilidades de conexión a la base, compartidas por los workers de larga duración."""
from django.db import close_old_connections, connection


def cerrar_conexiones_viejas():
    """`close_old_connections()`, pero NUNCA dentro de un bloque atómico.

    Los workers que corren fuera del ciclo request/response de Django
    (`run_mqtt_worker`, `run_meshcentral_worker`) tienen que descartar conexiones
    vencidas antes de tocar la base: si no, una conexión que el servidor ya cerró por
    timeout revienta el primer query después de un rato de silencio.

    El problema es que `close_old_connections()` cierra la conexión cuando el
    `autocommit` efectivo difiere del configurado — y eso es exactamente lo que pasa
    DENTRO de una transacción. Un `TestCase` de Django envuelve cada prueba en un
    bloque atómico, así que llamarlo ahí cierra la conexión de la prueba y todo lo que
    sigue en ese proceso falla con "the connection is closed".

    Con SQLite el síntoma no aparece y por eso pasó inadvertido; contra el PostgreSQL
    real (el mismo motor que producción) tumbaba 125 pruebas de una. Ver
    deploy/README-local.md.

    El resguardo no es solo para los tests: si algún día uno de estos handlers se
    llama desde dentro de un `transaction.atomic()`, cerrar la conexión ahí abortaría
    la transacción en curso en producción.
    """
    if connection.in_atomic_block:
        return
    close_old_connections()
