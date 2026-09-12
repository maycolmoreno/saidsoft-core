"""API de ingesta de sondeos de enlace.

Por qué existe: el sondeo ICMP tiene que correr desde un host con ruta hacia las IP de
las farmacias, y ese host **no es** el servidor central (100% de pérdida de ping,
confirmado el 24-ago-2026 — ver `apps.monitoreo.enlaces`). Cuando ese host además no
puede alcanzar la base de datos, la alternativa es que reporte por HTTP: una sonda
liviana mide y este endpoint persiste, reusando exactamente el mismo
`registrar_sondeo()` que usa el comando local.

Mismo esquema que la API móvil (`apps/mantenimiento/api_views.py`): DRF con
`TokenAuthentication`, que ya está configurado como default en `REST_FRAMEWORK`.

Sobre el permiso: exige `monitoreo.registrar_sondeo_enlace` y no solo estar
autenticado. El token de la sonda vive en una máquina de oficina, fuera del servidor —
si se filtra, tiene que servir para reportar mediciones y para nada más.
"""
import logging

from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.catalogo.models import Farmacia
from apps.cuentas.services import scope_por_unidad_negocio

from .enlaces import UMBRAL_BARRIDO_SOSPECHOSO_PCT, registrar_sondeo
from .serializers import SondeoEnlaceLoteSerializer

logger = logging.getLogger(__name__)


class FarmaciasASondearView(APIView):
    """GET /api/v1/monitoreo/enlaces/farmacias/ — qué sondear y con qué IP.

    Existe para que la sonda no tenga que mantener su propia copia de las IP. Esa copia
    es justamente lo que se desincroniza: una farmacia nueva o un cambio de IP quedarían
    sin monitorear hasta que alguien se acuerde de editar un CSV en otra máquina — el
    mismo problema que ya se pagó con las planillas de nodos (208 farmacias quedaron en
    el grupo PENDIENTE por eso).

    Solo devuelve las del alcance del usuario de la sonda, y solo código + IP: no hay
    motivo para que un token que vive fuera del servidor pueda leer el catálogo entero.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        if not request.user.has_perm('monitoreo.registrar_sondeo_enlace'):
            return Response(
                {'detail': 'Este token no puede consultar la lista de enlaces.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        farmacias = scope_por_unidad_negocio(
            Farmacia.objects.filter(activa=True).exclude(ip_router__isnull=True),
            request.user, 'unidad_negocio',
        ).order_by('codigo')
        return Response({
            'farmacias': [{'codigo': f.codigo, 'ip': str(f.ip_router)} for f in farmacias],
        })


class SondeoEnlaceIngestaView(APIView):
    """POST /api/v1/monitoreo/enlaces/sondeo/ — recibe un barrido completo de la sonda.

    Se recibe el barrido entero en una sola llamada, no una farmacia por request, porque
    la guarda de "barrido sospechoso" solo tiene sentido sobre el conjunto: si llegan
    caídas de a una, no hay forma de distinguir 704 caídas reales de una sonda que perdió
    su ruta. Con el lote completo, sí.
    """

    # Solo IsAuthenticated acá: el permiso concreto se verifica abajo. DjangoModelPermissions
    # NO sirve para esto — deduce el modelo del `queryset` de la vista y exige el `add_` de
    # ese modelo, que no es lo que queremos comprobar. `registrar_sondeo_enlace` es un
    # permiso custom y no entra en el mapeo CRUD que DRF hace por defecto.
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        if not request.user.has_perm('monitoreo.registrar_sondeo_enlace'):
            return Response(
                {'detail': 'Este token no puede reportar sondeos de enlace.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = SondeoEnlaceLoteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        resultados = serializer.validated_data['resultados']

        # Solo farmacias del alcance del usuario de la sonda. Una sonda de una unidad de
        # negocio no puede escribir el estado de otra ni siquiera equivocándose.
        codigos = [r['farmacia'] for r in resultados]
        visibles = {
            f.codigo: f
            for f in scope_por_unidad_negocio(
                Farmacia.objects.filter(codigo__in=codigos), request.user, 'unidad_negocio',
            )
        }
        desconocidas = sorted(set(codigos) - set(visibles))

        conocidos = [r for r in resultados if r['farmacia'] in visibles]
        if not conocidos:
            return Response(
                {'detail': 'Ninguna de las farmacias reportadas existe o está en tu alcance.',
                 'desconocidas': desconocidas},
                status=status.HTTP_400_BAD_REQUEST,
            )

        fallidos = sum(1 for r in conocidos if not r['alcanzable'])
        pct_fallido = 100 * fallidos / len(conocidos)
        if pct_fallido >= UMBRAL_BARRIDO_SOSPECHOSO_PCT:
            # Misma guarda que el barrido local, y por el mismo motivo: una sonda que
            # perdió su ruta reportaría toda la flota caída. Se rechaza sin escribir.
            logger.error(
                'Sondeo por API rechazado: %.0f%% de %d farmacias sin responder (usuario %s). '
                'Es la ruta de la sonda, no 704 caídas simultáneas.',
                pct_fallido, len(conocidos), request.user,
            )
            return Response({
                'abortado': True,
                'detail': (
                    f'{fallidos} de {len(conocidos)} farmacias sin responder ({pct_fallido:.0f}%). '
                    'Eso no son caídas simultáneas: la sonda perdió su ruta. No se registró nada.'
                ),
            }, status=status.HTTP_409_CONFLICT)

        activas = caidas = 0
        for resultado in conocidos:
            registrar_sondeo(
                visibles[resultado['farmacia']], resultado['alcanzable'], resultado.get('latencia_ms'),
            )
            if resultado['alcanzable']:
                activas += 1
            else:
                caidas += 1

        return Response({
            'abortado': False,
            'registrados': len(conocidos),
            'activas': activas,
            'caidas': caidas,
            'desconocidas': desconocidas,
        })
