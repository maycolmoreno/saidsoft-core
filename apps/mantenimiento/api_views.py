"""API móvil (apps Flutter). Cada acción delega en apps.mantenimiento.services
— la misma lógica que usa el panel HTMX — para no duplicar reglas de negocio."""
from rest_framework import generics, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from . import services
from .models import (
    ActividadChecklist, ConsentimientoMonitoreo, Mantenimiento, Notificacion, UbicacionTecnico,
)
from .serializers import (
    CerrarMantenimientoSerializer, ChecklistActualizarSerializer, ChecklistItemSerializer,
    ConsentimientoMonitoreoSerializer, FirmaMantenimientoSerializer, FirmarMantenimientoSerializer,
    ImagenAdjuntarSerializer, ImagenMantenimientoSerializer, MantenimientoCrearSerializer,
    MantenimientoDetalleSerializer, MantenimientoListSerializer, UbicacionTecnicoSerializer,
    ActividadChecklistSerializer, ActivoMovilSerializer, NotificacionSerializer, UsuarioActualSerializer,
)


class MantenimientoViewSet(viewsets.ReadOnlyModelViewSet):
    """Mantenimientos asignados al técnico autenticado (nunca los de otro técnico)."""
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Mantenimiento.objects.filter(tecnico=self.request.user).select_related(
            'cliente', 'tecnico',
        ).prefetch_related('equipos__equipo__farmacia', 'firmas', 'imagenes', 'eventos__usuario')

    def get_serializer_class(self):
        if self.action == 'list':
            return MantenimientoListSerializer
        if self.action == 'create':
            return MantenimientoCrearSerializer
        return MantenimientoDetalleSerializer

    def create(self, request, *args, **kwargs):
        """Autoservicio: el técnico crea su propio mantenimiento (igual que en InvTICS).

        `tecnico` siempre es request.user, nunca viene del payload — a diferencia
        del formulario del panel, donde un coordinador puede asignarlo a otra persona.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        d = serializer.validated_data
        try:
            mantenimiento = services.crear_mantenimiento_manual(
                equipos=list(d['equipos']), tecnico=request.user, cliente=d['cliente'],
                tipo_mantenimiento=d['tipo_mantenimiento'],
                descripcion=d['descripcion'], fecha_programada=d['fecha_programada'],
                estado_general=d['estado_general'], mantenimiento_programado=d.get('mantenimiento_programado'),
                usuario=request.user,
            )
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(MantenimientoDetalleSerializer(mantenimiento).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['get'])
    def checklist(self, request, pk=None):
        mantenimiento = self.get_object()
        items = ActividadChecklist.objects.filter(activo=True).order_by('orden', 'nombre')
        realizadas = {ar.actividad_id: ar.realizada for ar in mantenimiento.actividades_realizadas.all()}
        data = [{'item': item, 'realizada': realizadas.get(item.pk, False)} for item in items]
        return Response(ChecklistItemSerializer(data, many=True).data)

    @action(detail=True, methods=['post'])
    def iniciar(self, request, pk=None):
        mantenimiento = self.get_object()
        try:
            services.iniciar_mantenimiento(mantenimiento=mantenimiento, usuario=request.user)
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(MantenimientoDetalleSerializer(mantenimiento).data)

    @action(detail=True, methods=['post'], url_path='checklist/actualizar')
    def actualizar_checklist(self, request, pk=None):
        mantenimiento = self.get_object()
        serializer = ChecklistActualizarSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.registrar_actividad_checklist(
            mantenimiento=mantenimiento, actividad=serializer.validated_data['actividad_id'],
            realizada=serializer.validated_data['realizada'], usuario=request.user,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['post'])
    def cerrar(self, request, pk=None):
        mantenimiento = self.get_object()
        serializer = CerrarMantenimientoSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            services.cerrar_mantenimiento(
                mantenimiento=mantenimiento, resultado_tecnico=serializer.validated_data['resultado_tecnico'],
                usuario=request.user,
                tiempo_real_minutos=serializer.validated_data.get('tiempo_real_minutos'),
                estado_general=serializer.validated_data.get('estado_general', ''),
            )
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(MantenimientoDetalleSerializer(mantenimiento).data)

    @action(detail=True, methods=['post'])
    def firmar(self, request, pk=None):
        mantenimiento = self.get_object()
        serializer = FirmarMantenimientoSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        firma = services.firmar_mantenimiento(
            mantenimiento=mantenimiento, tipo_firma=serializer.validated_data['tipo_firma'],
            firma_base64=serializer.validated_data['firma_base64'], usuario=request.user,
            ip_origen=request.META.get('REMOTE_ADDR'),
        )
        return Response(FirmaMantenimientoSerializer(firma).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='imagenes')
    def adjuntar_imagen(self, request, pk=None):
        mantenimiento = self.get_object()
        serializer = ImagenAdjuntarSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        imagen = services.adjuntar_imagen_mantenimiento(
            mantenimiento=mantenimiento, archivo=serializer.validated_data['archivo'], usuario=request.user,
        )
        return Response(ImagenMantenimientoSerializer(imagen).data, status=status.HTTP_201_CREATED)


class ConsentimientoMonitoreoView(generics.GenericAPIView):
    """GET: último consentimiento del usuario autenticado. POST: registra uno nuevo."""
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = ConsentimientoMonitoreoSerializer

    def get(self, request):
        ultimo = ConsentimientoMonitoreo.objects.filter(usuario=request.user).order_by('-timestamp').first()
        if ultimo is None:
            return Response({'aceptado': False})
        return Response(ConsentimientoMonitoreoSerializer(ultimo).data)

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        consentimiento = ConsentimientoMonitoreo.objects.create(
            usuario=request.user, ip=request.META.get('REMOTE_ADDR'), **serializer.validated_data,
        )
        return Response(ConsentimientoMonitoreoSerializer(consentimiento).data, status=status.HTTP_201_CREATED)


class UbicacionTecnicoView(generics.ListCreateAPIView):
    """Registra/consulta posiciones GPS del técnico autenticado.

    Exige un ConsentimientoMonitoreo vigente (aceptado=True) antes de aceptar
    una posición — igual que el flujo de consentimiento legal en InvTICS.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = UbicacionTecnicoSerializer

    def get_queryset(self):
        return UbicacionTecnico.objects.filter(usuario=self.request.user).order_by('-timestamp_captura')[:100]

    def create(self, request, *args, **kwargs):
        tiene_consentimiento = ConsentimientoMonitoreo.objects.filter(
            usuario=request.user, aceptado=True,
        ).exists()
        if not tiene_consentimiento:
            return Response(
                {'detail': 'Falta registrar el consentimiento de monitoreo antes de enviar ubicación.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(usuario=request.user)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class ActividadChecklistView(generics.ListAPIView):
    """Catálogo global de actividades de checklist.

    La app lo necesita al CREAR un mantenimiento, cuando todavía no existe un id
    contra el que pedir `/mantenimientos/{id}/checklist/`. Es solo lectura: el
    catálogo se administra desde el panel.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = ActividadChecklistSerializer
    pagination_class = None

    def get_queryset(self):
        return ActividadChecklist.objects.filter(activo=True).order_by('orden', 'nombre')


class UsuarioActualView(generics.GenericAPIView):
    """Identidad y permisos del usuario autenticado, para que la app móvil sepa qué
    mostrar y qué habilitar.

    Hace falta porque `obtain_auth_token` de DRF devuelve solo el token: sin esto la
    app no sabe el nombre real del técnico ni qué acciones puede hacer, y tendría que
    o mostrar todo (y fallar con 403 al tocar), o adivinar por el nombre de usuario.

    Los permisos van con los mismos codenames de Django que ya usa el panel, para que
    la app y la web habiliten exactamente lo mismo sin una segunda tabla de roles que
    se desincronice.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = UsuarioActualSerializer

    def get(self, request):
        return Response(self.get_serializer(request.user).data)


class EquipoListView(generics.ListAPIView):
    """Equipos visibles para el técnico, acotados a sus unidades de negocio.

    Mismo alcance por tenant que el panel (apps.cuentas.services): un técnico de MIA
    no ve los activos de San Gregorio. Excluye los dados de baja, que no son
    accionables en campo.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = ActivoMovilSerializer
    pagination_class = None

    def get_queryset(self):
        from apps.activos.models import Activo
        from apps.cuentas.services import scope_por_unidad_negocio

        queryset = Activo.objects.exclude(estado=Activo.Estado.DADO_DE_BAJA).select_related(
            'marca', 'categoria', 'farmacia', 'estacion',
        ).order_by('codigo')
        return scope_por_unidad_negocio(queryset, self.request.user, 'unidad_negocio')


class NotificacionListView(generics.ListAPIView):
    """Bandeja del usuario autenticado -- nunca la de otro."""
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = NotificacionSerializer
    pagination_class = None

    def get_queryset(self):
        return Notificacion.objects.filter(usuario=self.request.user).select_related('mantenimiento')


class NotificacionConteoView(generics.GenericAPIView):
    """Cantidad de no leídas, para el badge del dashboard."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        total = Notificacion.objects.filter(usuario=request.user, leida=False).count()
        return Response({'count': total})


class NotificacionLeerView(generics.GenericAPIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        actualizadas = Notificacion.objects.filter(
            pk=pk, usuario=request.user, leida=False,
        ).update(leida=True)
        if not actualizadas:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(status=status.HTTP_204_NO_CONTENT)
