"""API móvil (apps Flutter). Cada acción delega en apps.mantenimiento.services
— la misma lógica que usa el panel HTMX — para no duplicar reglas de negocio."""
from django.utils import timezone
from rest_framework import generics, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from . import services
from .api_permissions import PermisoDeclarado
from .models import (
    AccionOfflineAplicada, ActividadChecklist, ConsentimientoMonitoreo, Mantenimiento, Notificacion,
    UbicacionTecnico, VisitaTecnica,
)
from .serializers import (
    CancelarMantenimientoSerializer, CerrarMantenimientoSerializer, ChecklistActualizarSerializer,
    ChecklistItemSerializer, RepuestoUtilizadoSerializer,
    ConsentimientoMonitoreoSerializer, FirmaMantenimientoSerializer, FirmarMantenimientoSerializer,
    ImagenAdjuntarSerializer, ImagenMantenimientoSerializer, MantenimientoCrearSerializer,
    MantenimientoDetalleSerializer, MantenimientoListSerializer, UbicacionTecnicoSerializer,
    ActividadChecklistSerializer, ActivoMovilSerializer, CerrarVisitaSerializer, NotificacionSerializer,
    ActivoCrearSerializer, CatalogosSerializer, UsuarioActualSerializer, VisitaTecnicaSerializer,
    AccionDiferidaSerializer,
)


def _origen_id(request):
    """id de la fila en la ColaOffline del teléfono, si la acción vino de ahí.

    Opcional por el mismo motivo que `ocurrido_en`: los APKs viejos no lo mandan y
    tienen que seguir funcionando (sin protección contra doble aplicación, que es lo
    que tienen hoy).
    """
    serializer = AccionDiferidaSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data.get('origen_id')


def _idempotente(request, tipo, ejecutar):
    """Aplica `ejecutar()` una sola vez por acción encolada.

    El caso que cubre: `api.dart` traduce su timeout de 20 s a `SinConexion`, así que
    una petición que el servidor SÍ procesó —pero cuya respuesta se perdió— se reencola
    y se reintenta. Sin esto, el reintento duplicaba el hecho o rebotaba con un error
    que dejaba la acción trabada en el teléfono para siempre.

    Se recuerdan las respuestas TERMINALES: 2xx (se aplicó) y 409 (hay un conflicto ya
    registrado). Un 400 no se recuerda porque no cambió nada, y reintentarlo es inocuo.
    """
    origen_id = _origen_id(request)
    if origen_id is None:
        return ejecutar()

    previa = AccionOfflineAplicada.objects.filter(usuario=request.user, origen_id=origen_id).first()
    if previa is not None:
        return Response(previa.respuesta_json, status=previa.estado_http)

    respuesta = ejecutar()
    if respuesta.status_code < 300 or respuesta.status_code == status.HTTP_409_CONFLICT:
        # `get_or_create` y no `create`: dos reintentos simultáneos del mismo teléfono
        # llegan a esta línea a la vez, y el constraint los tiene que resolver sin 500.
        AccionOfflineAplicada.objects.get_or_create(
            usuario=request.user, origen_id=origen_id,
            defaults={
                'tipo': tipo, 'respuesta_json': respuesta.data,
                'estado_http': respuesta.status_code,
            },
        )
    return respuesta


def _conflicto(exc):
    """409 y no 400: la app lo distingue por `codigo` y deja de reintentar."""
    return Response(exc.como_respuesta(), status=status.HTTP_409_CONFLICT)


def _ocurrido_en(request):
    """Hora real de una acción que pudo venir de la cola offline de la app.

    Se valida siempre (ventana en serializers.py) y es opcional: los teléfonos que
    todavía no se actualizaron no mandan el campo y siguen fechándose con la hora del
    servidor, como hasta ahora. Ver el comentario largo en serializers.py.
    """
    serializer = AccionDiferidaSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data['ocurrido_en']


class MantenimientoViewSet(viewsets.ReadOnlyModelViewSet):
    """Mantenimientos asignados al técnico autenticado (nunca los de otro técnico)."""

    permission_classes = [PermisoDeclarado]
    # Espejo exacto de apps/panel/views/mantenimiento.py. Las sub-acciones
    # (checklist/firma/imagen/repuesto) van con `change_mantenimiento` y no con un
    # permiso propio, igual que en el panel -- ver el comentario de seed_permisos.py.
    permisos_por_accion = {
        'list': ['mantenimiento.view_mantenimiento'],
        'retrieve': ['mantenimiento.view_mantenimiento'],
        'checklist': ['mantenimiento.view_mantenimiento'],
        'create': ['mantenimiento.add_mantenimiento'],
        'iniciar': ['mantenimiento.change_mantenimiento'],
        'actualizar_checklist': ['mantenimiento.change_mantenimiento'],
        'cerrar': ['mantenimiento.change_mantenimiento'],
        'cancelar': ['mantenimiento.change_mantenimiento'],
        'repuestos': ['mantenimiento.change_mantenimiento'],
        'firmar': ['mantenimiento.change_mantenimiento'],
        'adjuntar_imagen': ['mantenimiento.change_mantenimiento'],
    }

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

        def ejecutar():
            try:
                services.iniciar_mantenimiento(
                    mantenimiento=mantenimiento, usuario=request.user,
                    ocurrido_en=_ocurrido_en(request),
                )
            except ValueError as exc:
                return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
            return Response(MantenimientoDetalleSerializer(mantenimiento).data)

        return _idempotente(request, 'iniciar_mantenimiento', ejecutar)

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
        d = serializer.validated_data

        def ejecutar():
            try:
                services.cerrar_mantenimiento(
                    mantenimiento=mantenimiento, resultado_tecnico=d['resultado_tecnico'],
                    usuario=request.user,
                    tiempo_real_minutos=d.get('tiempo_real_minutos'),
                    estado_general=d.get('estado_general', ''),
                    ocurrido_en=d.get('ocurrido_en'),
                )
            except services.ConflictoDeEstado as exc:
                # El trabajo del tecnico se guarda ANTES de contestarle que no se pudo
                # aplicar: si se perdiera aca, el 409 seria solo una forma mas prolija
                # de tirarlo a la basura.
                services.registrar_cierre_en_conflicto(
                    mantenimiento=mantenimiento, tecnico=request.user,
                    payload={
                        'resultado_tecnico': d['resultado_tecnico'],
                        'tiempo_real_minutos': d.get('tiempo_real_minutos'),
                        'estado_general': d.get('estado_general', ''),
                    },
                    motivo=exc.mensaje, ocurrido_en=d.get('ocurrido_en'),
                )
                return _conflicto(exc)
            except ValueError as exc:
                return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
            return Response(MantenimientoDetalleSerializer(mantenimiento).data)

        return _idempotente(request, 'cerrar_mantenimiento', ejecutar)

    @action(detail=True, methods=['post'])
    def cancelar(self, request, pk=None):
        """Cancelar desde el campo: el equipo no estaba, era falsa alarma, se duplicó.

        Faltaba, y no era menor: un mantenimiento abierto impide abrir otro sobre el
        mismo equipo, así que sin esto un error de carga dejaba el equipo trabado hasta
        que alguien entrara al panel.
        """
        mantenimiento = self.get_object()
        serializer = CancelarMantenimientoSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        def ejecutar():
            try:
                services.cancelar_mantenimiento(
                    mantenimiento=mantenimiento, motivo=serializer.validated_data['motivo'],
                    usuario=request.user,
                )
            except services.ConflictoDeEstado as exc:
                return _conflicto(exc)
            except ValueError as exc:
                return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
            return Response(MantenimientoDetalleSerializer(mantenimiento).data)

        return _idempotente(request, 'cancelar_mantenimiento', ejecutar)

    @action(detail=True, methods=['post'])
    def repuestos(self, request, pk=None):
        """Registrar un repuesto/consumible gastado en la intervención.

        Lo gasta el técnico en campo, así que tenía que poder cargarlo él: hasta ahora
        solo existía en el panel, y el stock quedaba mintiendo hasta que alguien de
        oficina lo transcribiera.
        """
        mantenimiento = self.get_object()
        serializer = RepuestoUtilizadoSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        d = serializer.validated_data
        try:
            services.registrar_repuesto_utilizado(
                mantenimiento=mantenimiento, tipo_consumible=d['tipo_consumible'],
                cantidad=d['cantidad'], bodega=d.get('bodega'),
                costo_unitario=d.get('costo_unitario'), usuario=request.user,
            )
        except ValueError as exc:
            # Stock insuficiente entra por acá: es un dato que el técnico puede
            # corregir, no un error del sistema.
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(MantenimientoDetalleSerializer(mantenimiento).data)

    @action(detail=True, methods=['post'])
    def firmar(self, request, pk=None):
        mantenimiento = self.get_object()
        serializer = FirmarMantenimientoSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        def ejecutar():
            firma = services.firmar_mantenimiento(
                mantenimiento=mantenimiento, tipo_firma=serializer.validated_data['tipo_firma'],
                firma_base64=serializer.validated_data['firma_base64'], usuario=request.user,
                ip_origen=request.META.get('REMOTE_ADDR'),
                ocurrido_en=serializer.validated_data.get('ocurrido_en'),
            )
            return Response(FirmaMantenimientoSerializer(firma).data, status=status.HTTP_201_CREATED)

        return _idempotente(request, 'firmar_mantenimiento', ejecutar)

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

    # `IsAuthenticated` a secas, y es deliberado: esto es el consentimiento legal de la
    # PROPIA persona sobre su propia ubicación. Exigir un permiso para poder consentir
    # (o para revocar) invertiría el sentido del control, y además dejaría la pantalla
    # de GPS sin poder completar su onboarding.
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
    permission_classes = [PermisoDeclarado]
    # No tiene equivalente en el panel (la ubicación es una superficie solo de la app),
    # así que se usan los codenames naturales del modelo. La app gatea toda la pantalla
    # con `add_ubicaciontecnico` (ver Permiso.enviarUbicacion en sesion.dart).
    permisos_por_metodo = {
        'GET': ['mantenimiento.view_ubicaciontecnico'],
        'POST': ['mantenimiento.add_ubicaciontecnico'],
    }
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

        def ejecutar():
            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            serializer.save(usuario=request.user)
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        # Tambien pasa por la idempotencia: una posicion duplicada no rompe el calculo
        # de distancia (que toma el minimo), pero infla una tabla que ya crece sola y
        # que hoy no tiene purga.
        return _idempotente(request, 'enviar_ubicacion', ejecutar)


class ActividadChecklistView(generics.ListAPIView):
    """Catálogo global de actividades de checklist.

    La app lo necesita al CREAR un mantenimiento, cuando todavía no existe un id
    contra el que pedir `/mantenimientos/{id}/checklist/`. Es solo lectura: el
    catálogo se administra desde el panel.
    """
    permission_classes = [PermisoDeclarado]
    permisos_por_metodo = {'GET': ['mantenimiento.view_mantenimiento']}
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
    # Identidad del propio usuario: sin esto no puede ni saber qué permisos tiene.
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
    permission_classes = [PermisoDeclarado]
    # Mismo codename que `activos_lista` del panel.
    permisos_por_metodo = {'GET': ['activos.view_activo']}
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

    # Sin codename a propósito, mismo criterio que el panel (`notificaciones_lista` es
    # el único @login_required sin @permission_required) y que la app, que no gatea la
    # pestaña Avisos: la bandeja es del propio usuario, no un modelo de negocio.
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
    permission_classes = [PermisoDeclarado]
    permisos_por_accion = {
        'list': ['mantenimiento.view_visitatecnica'],
        'retrieve': ['mantenimiento.view_visitatecnica'],
        'iniciar': ['mantenimiento.change_visitatecnica'],
        'cerrar': ['mantenimiento.change_visitatecnica'],
    }
    serializer_class = VisitaTecnicaSerializer
    pagination_class = None

    def get_queryset(self):
        return VisitaTecnica.objects.filter(tecnico=self.request.user).select_related('farmacia')

    @action(detail=True, methods=['post'])
    def iniciar(self, request, pk=None):
        visita = self.get_object()

        def ejecutar():
            try:
                services.iniciar_visita_tecnica(
                    visita=visita, usuario=request.user, ocurrido_en=_ocurrido_en(request),
                )
            except services.ConflictoDeEstado as exc:
                return _conflicto(exc)
            except ValueError as exc:
                return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
            return Response(self.get_serializer(visita).data)

        return _idempotente(request, 'iniciar_visita', ejecutar)

    @action(detail=True, methods=['post'])
    def cerrar(self, request, pk=None):
        visita = self.get_object()
        serializer = CerrarVisitaSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        def ejecutar():
            try:
                services.cerrar_visita_tecnica(
                    visita=visita, usuario=request.user,
                    observaciones=serializer.validated_data.get('observaciones', ''),
                    ocurrido_en=serializer.validated_data.get('ocurrido_en'),
                )
            except services.ConflictoDeEstado as exc:
                return _conflicto(exc)
            except ValueError as exc:
                return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
            return Response(self.get_serializer(visita).data)

        return _idempotente(request, 'cerrar_visita', ejecutar)


class CatalogosView(generics.GenericAPIView):
    """Todos los catálogos que necesitan los formularios de la app, en una respuesta.

    La app los cachea: en una farmacia con enlace intermitente, cinco llamadas para
    llenar un formulario son cinco oportunidades de fallar.
    """
    permission_classes = [PermisoDeclarado]
    # Catálogos de los formularios de la app (alta de mantenimiento y de equipo). Ya
    # vienen acotados por unidad de negocio; el codename es el del flujo que los pide.
    permisos_por_metodo = {'GET': ['mantenimiento.view_mantenimiento']}
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
