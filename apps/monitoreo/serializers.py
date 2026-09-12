"""Serializers de la API de monitoreo. Ver `apps.monitoreo.api_views`."""
from rest_framework import serializers


class SondeoEnlaceSerializer(serializers.Serializer):
    """El resultado de un ping a una farmacia, tal como lo reporta la sonda externa."""

    farmacia = serializers.CharField(max_length=15, help_text='Código de la farmacia, ej. ML001.')
    alcanzable = serializers.BooleanField()
    latencia_ms = serializers.FloatField(
        required=False, allow_null=True, min_value=0,
        help_text='Solo si respondió. Vacío es válido: hay salidas de `ping` de las que no se '
                  'puede extraer el tiempo, y perder la latencia no invalida el sondeo.',
    )


class SondeoEnlaceLoteSerializer(serializers.Serializer):
    """Un barrido completo.

    Se recibe el lote entero y no una farmacia por request a propósito: la guarda de
    "barrido sospechoso" (¿se cayó la flota o se cayó la ruta de la sonda?) solo se puede
    evaluar sobre el conjunto.
    """

    resultados = serializers.ListField(
        child=SondeoEnlaceSerializer(), allow_empty=False,
        # La flota son ~704 sitios; el margen deja lugar a crecer sin que un barrido
        # legítimo se rechace, y acota un payload absurdo.
        max_length=2000,
    )

    def validate_resultados(self, valor):
        codigos = [r['farmacia'] for r in valor]
        duplicados = {c for c in codigos if codigos.count(c) > 1}
        if duplicados:
            # Dos resultados de la misma farmacia en un barrido significan que la sonda
            # está mal armada: cuál gana sería arbitrario, y uno de los dos contaría mal
            # en la guarda de barrido sospechoso.
            raise serializers.ValidationError(
                f'Farmacias repetidas en el mismo barrido: {", ".join(sorted(duplicados))}.',
            )
        return valor
