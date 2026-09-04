"""API móvil (apps Flutter). Cada acción delega en apps.mantenimiento.services
— la misma lógica que usa el panel HTMX — para no duplicar reglas de negocio."""
from django.utils import timezone
from rest_framework import generics, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from . import services
from .models import (
    ActividadChecklist, ConsentimientoMonitoreo, Mantenimiento, Notificacion, UbicacionTecnico,
    VisitaTecnica,
)
from .serializers import (
    CerrarMantenimientoSerializer, ChecklistActualizarSerializer, ChecklistItemSerializer,
    ConsentimientoMonitoreoSerializer, FirmaMantenimientoSerializer, FirmarMantenimientoSerializer,
    ImagenAdjuntarSerializer, ImagenMantenimientoSerializer, MantenimientoCrearSerializer,
    MantenimientoDetalleSerializer, MantenimientoListSerializer, UbicacionTecnicoSerializer,
    ActividadChecklistSerializer, ActivoMovilSerializer, CerrarVisitaSerializer, NotificacionSerializer,
    ActivoCrearSerializer, CatalogosSerializer, UsuarioActualSerializer, VisitaTecnicaSerializer,
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
                equipos=list(d['equipos']), tecnico=request.user, cliente=d.get('cliente'),
                tipo_mantenimiento=d['tipo_mantenimiento'],
                descripcion=d['descripcion'],
                fecha_programada=d.get('fecha_programada') or timezone.now(),
                estado_general=d['estado_general'], prioridad=d.get('prioridad'),
                mantenimiento_programado=d.get('mantenimiento_programado'),
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
        from apps.cuentas.services import scope_opcional_por_unidad_negocio

        queryset = Activo.objects.exclude(estado=Activo.Estado.DADO_DE_BAJA).select_related(
            'marca', 'categoria', 'farmacia', 'estacion', 'colaborador_actual',
        ).order_by('codigo')

        # Un solo campo de busqueda que entiende todo lo que el tecnico tiene a mano:
        # la etiqueta del equipo, el codigo de la farmacia donde esta parado, o el
        # nombre de la persona que lo usa. Obligarlo a elegir ANTES por que criterio
        # busca es trabajo que la consulta puede hacer sola.
        buscar = self.request.query_params.get('buscar', '').strip()
        if buscar:
            from django.db.models import Q
            queryset = queryset.filter(
                Q(codigo__icontains=buscar)
                | Q(numero_serie__icontains=buscar)
                | Q(modelo__icontains=buscar)
                | Q(farmacia__codigo__icontains=buscar)
                | Q(farmacia__nombre__icontains=buscar)
                | Q(colaborador_actual__nombre__icontains=buscar),
            )
        # Filtros explicitos, para listar TODO lo de una farmacia o de una persona sin
        # depender de que el texto coincida.
        farmacia = self.request.query_params.get('farmacia')
        if farmacia:
            queryset = queryset.filter(farmacia_id=farmacia)
        cliente = self.request.query_params.get('cliente')
        if cliente:
            queryset = queryset.filter(colaborador_actual_id=cliente)
        # `scope_opcional_*` y no la variante estricta: `Activo.unidad_negocio` es
        # nullable y el panel (activos_lista) ya trata el vacío como "compartido,
        # visible para todos". La app usaba la estricta, que EXCLUYE los nulos, así que
        # el mismo equipo se veía en la web y desaparecía en el celular. Como
        # `registrar_ingreso` no setea unidad_negocio, eso son TODOS los equipos: un
        # técnico con tenant acotado no habría podido abrir ningún mantenimiento.
        return scope_opcional_por_unidad_negocio(queryset, self.request.user, 'unidad_negocio')


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


class VisitaTecnicaViewSet(viewsets.ReadOnlyModelViewSet):
    """Visitas asignadas al técnico autenticado (nunca las de otro).

    Las transiciones delegan en services, igual que el panel: la app es transporte,
    no reimplementa el ciclo de vida.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = VisitaTecnicaSerializer
    pagination_class = None

    def get_queryset(self):
        return VisitaTecnica.objects.filter(tecnico=self.request.user).select_related('farmacia')

    @action(detail=True, methods=['post'])
    def iniciar(self, request, pk=None):
        visita = self.get_object()
        try:
            services.iniciar_visita_tecnica(visita=visita, usuario=request.user)
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(self.get_serializer(visita).data)

    @action(detail=True, methods=['post'])
    def cerrar(self, request, pk=None):
        visita = self.get_object()
        serializer = CerrarVisitaSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            services.cerrar_visita_tecnica(
                visita=visita, usuario=request.user,
                observaciones=serializer.validated_data.get('observaciones', ''),
            )
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(self.get_serializer(visita).data)


class CatalogosView(generics.GenericAPIView):
    """Todos los catálogos que necesitan los formularios de la app, en una respuesta.

    La app los cachea: en una farmacia con enlace intermitente, cinco llamadas para
    llenar un formulario son cinco oportunidades de fallar.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = CatalogosSerializer

    def get(self, request):
        return Response(self.get_serializer({}).data)


class ActivoCrearView(generics.CreateAPIView):
    """Alta de un equipo desde el campo.

    Delega en apps.activos.services.registrar_ingreso -- la misma función que usa el
    panel -- para no tener dos reglas distintas sobre cómo nace un activo.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = ActivoCrearSerializer

    def create(self, request, *args, **kwargs):
        if not request.user.has_perm('activos.add_activo'):
            return Response(
                {'detail': 'No tenes permiso para registrar equipos.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        d = serializer.validated_data

        from apps.activos import services as activos_services

        try:
            activo = activos_services.registrar_ingreso(
                tipo=d['tipo'], marca=d['marca'], categoria=d['categoria'],
                modelo=d['modelo'], numero_serie=d['numero_serie'],
                procesador=d['procesador'], ram_gb=d['ram_gb'],
                almacenamiento_gb=d['almacenamiento_gb'], codigo_sap=d['codigo_sap'],
                fecha_compra=None, vencimiento_garantia=None, orden_compra=None,
                bodega=d['bodega'], farmacia=d['farmacia'], usuario=request.user,
            )
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(ActivoMovilSerializer(activo).data, status=status.HTTP_201_CREATED)
