import ast
import pathlib

from django.apps import apps as registro_de_apps
from django.conf import settings
from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase

from apps.catalogo.models import Farmacia, Grupo, UnidadNegocio

from .models import ATRIBUTOS_RUTA_TENANT, EventoAuditoria, registrar_evento


class RegistrarEventoTests(TestCase):
    def setUp(self):
        self.usuario = User.objects.create_user(username='u', password='x')
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg)

    def test_registra_accion_y_objeto(self):
        registrar_evento(usuario=self.usuario, accion='farmacia.editar', objeto=self.farmacia)
        evento = EventoAuditoria.objects.get(accion='farmacia.editar')
        self.assertEqual(evento.usuario, self.usuario)
        self.assertEqual(evento.modelo, 'catalogo.Farmacia')
        self.assertEqual(evento.objeto_id, str(self.farmacia.pk))
        self.assertEqual(evento.objeto_repr, str(self.farmacia))

    def test_sin_objeto_deja_campos_vacios(self):
        registrar_evento(usuario=self.usuario, accion='sistema.arranque')
        evento = EventoAuditoria.objects.get(accion='sistema.arranque')
        self.assertEqual(evento.modelo, '')
        self.assertEqual(evento.objeto_id, '')

    def test_detalle_se_guarda_como_json(self):
        registrar_evento(usuario=self.usuario, accion='farmacia.editar', detalle={'campo': 'nombre'})
        evento = EventoAuditoria.objects.get(accion='farmacia.editar')
        self.assertEqual(evento.detalle, {'campo': 'nombre'})

    def test_toma_la_ip_del_request(self):
        request = RequestFactory().get('/', REMOTE_ADDR='10.0.0.5')
        registrar_evento(usuario=self.usuario, accion='farmacia.editar', request=request)
        evento = EventoAuditoria.objects.get(accion='farmacia.editar')
        self.assertEqual(evento.ip_address, '10.0.0.5')

    def test_usuario_none_se_registra_como_sistema(self):
        registrar_evento(usuario=None, accion='sistema.tarea_programada')
        evento = EventoAuditoria.objects.get(accion='sistema.tarea_programada')
        self.assertIsNone(evento.usuario)


class ContratoTenantDeAuditoriaTests(TestCase):
    """Vigila el contrato implícito de `_resolver_unidad_negocio`.

    El resolvedor busca el tenant por NOMBRE de atributo (`unidad_negocio`, `farmacia`,
    `estacion`, `equipo`). Un modelo que no exponga ninguno se audita con
    `unidad_negocio=None`, y un evento sin tenant es visible para TODAS las unidades
    (ver el help_text del campo). Eso pasa en silencio: no hay excepción, no hay log, y
    el evento queda escrito igual.

    Estas pruebas no cambian el comportamiento — lo hacen visible. Cada modelo que hoy
    se audita está clasificado abajo, y agregar uno nuevo que resuelva a None obliga a
    ponerlo en una de las dos listas y, al hacerlo, a decidir si corresponde.
    """

    def setUp(self):
        self.usuario = User.objects.create_user(username='u_contrato', password='x')
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX900')
        self.farmacia = Farmacia.objects.create(codigo='ML900', grupo=grupo, unidad_negocio=self.sg)

    # Modelos que se auditan hoy y SÍ llegan al tenant, con la ruta por la que lo hacen.
    # Verificado contra las 101 llamadas a registrar_evento en código de producción.
    CON_TENANT = {
        'catalogo.Estacion': 'farmacia',
        'catalogo.Farmacia': 'unidad_negocio',
        'activos.Activo': 'unidad_negocio',
        'activos.Bodega': 'unidad_negocio',
        'activos.OrdenCompra': 'unidad_negocio',
        'activos.Colaborador': 'unidad_negocio',
        'mantenimiento.Mantenimiento': 'unidad_negocio',
        'mantenimiento.MantenimientoProgramado': 'equipo',
        'mantenimiento.ActividadPlanificada': 'unidad_negocio',
        'mantenimiento.CierreEnConflicto': 'unidad_negocio',
        'mantenimiento.VisitaTecnica': 'unidad_negocio',
        'monitoreo.Alerta': 'estacion',
        'monitoreo.ReglaAlerta': 'unidad_negocio',
        'monitoreo.VentanaMantenimiento': 'unidad_negocio',
        'scripts.Script': 'unidad_negocio',
        'scripts.EjecucionScript': 'unidad_negocio',
        'scripts.ScriptProgramado': 'unidad_negocio',
        'software.AplicacionCatalogo': 'unidad_negocio',
        'software.SolicitudInstalacion': 'unidad_negocio',
        'software.InventarioProgramado': 'unidad_negocio',
        'despliegues.Despliegue': 'unidad_negocio',
        'aperturas.Apertura': 'unidad_negocio',
        'cumplimiento.ActividadCumplimiento': None,  # ver COMPARTIDOS_LEGITIMOS
    }

    # Resuelven a None y ESTÁ BIEN: no tienen un tenant único que derivar.
    COMPARTIDOS_LEGITIMOS = {
        'catalogo.VersionAgente':
            'Un build del agente es uno solo para toda la flota, de cualquier cliente.',
        'cumplimiento.ActividadCumplimiento':
            'M2M `unidades_negocio`: una actividad se dirige a VARIAS unidades a la vez. '
            'Es el caso que el help_text de EventoAuditoria.unidad_negocio ya nombra.',
        'mqtt_worker.EnrolamientoRechazado':
            'Se rechazó justamente porque no se encontró su farmacia: no hay tenant que '
            'derivar, y eso es el dato, no un hueco.',
        'activos.OrdenCompraDetalle':
            'Línea de una OrdenCompra, cuyo propio `unidad_negocio` es nullable '
            '(compartida). Hereda el criterio del padre.',
        'software.VersionAplicacion':
            'Versión de una AplicacionCatalogo, cuyo `unidad_negocio` es nullable '
            '(catálogo compartido). Hereda el criterio del padre.',
        'auth.User':
            'Los eventos de MFA son sobre la cuenta, no sobre datos de un cliente.',
    }

    # Resuelven a None pero NO porque sean compartidos: tienen un tenant único en los
    # datos y el resolvedor no lo alcanza porque el campo se llama de otra forma.
    # Documentado, no corregido: cambiar la resolución cambiaría la visibilidad de
    # eventos ya escritos, y esa es una decisión aparte de hacer visible el caso.
    SIN_TENANT_POR_NOMBRE_DEL_CAMPO = {
        'viaticos.ReporteViatico':
            'Tiene `farmacia_visitada` (FK a Farmacia) y `colaborador` (FK con '
            'unidad_negocio). El resolvedor busca `farmacia`, no `farmacia_visitada`, '
            'así que todo evento viatico.* queda sin tenant y visible para todos.',
        'viaticos.ColaboradorZona':
            'Tiene `colaborador`, que sí lleva a unidad_negocio. El resolvedor no '
            'prueba esa ruta.',
    }

    def _resolver_atributo(self, modelo):
        """El atributo por el que `_resolver_unidad_negocio` encontraría el tenant."""
        campos = {f.name for f in modelo._meta.get_fields()}
        for atributo in ATRIBUTOS_RUTA_TENANT:
            if atributo in campos or isinstance(getattr(modelo, atributo, None), property):
                return atributo
        return None

    def test_los_modelos_declarados_con_tenant_siguen_teniendo_la_ruta(self):
        """Renombrar o quitar el campo que lleva al tenant rompe la auditoría en silencio.

        Es el caso que más fácil se cuela: el modelo sigue funcionando, el panel sigue
        filtrando (usa su propio lookup explícito), y lo único que cambia es que los
        eventos empiezan a escribirse sin tenant.
        """
        for etiqueta, esperado in self.CON_TENANT.items():
            if esperado is None:
                continue  # clasificado en COMPARTIDOS_LEGITIMOS
            with self.subTest(modelo=etiqueta):
                modelo = registro_de_apps.get_model(etiqueta)
                self.assertEqual(
                    self._resolver_atributo(modelo), esperado,
                    f'{etiqueta} ya no llega al tenant por `{esperado}`. Si el campo se '
                    f'renombró, actualizá CON_TENANT; si desapareció, los eventos de este '
                    f'modelo quedaron visibles para todas las unidades de negocio.',
                )

    def test_los_modelos_sin_tenant_estan_declarados_y_justificados(self):
        """Nada resuelve a None sin una línea que diga por qué."""
        declarados = {**self.COMPARTIDOS_LEGITIMOS, **self.SIN_TENANT_POR_NOMBRE_DEL_CAMPO}
        for etiqueta, motivo in declarados.items():
            with self.subTest(modelo=etiqueta):
                modelo = registro_de_apps.get_model(etiqueta)
                self.assertIsNone(
                    self._resolver_atributo(modelo),
                    f'{etiqueta} ahora SÍ llega al tenant: sacalo de la lista de '
                    f'"sin tenant" y agregalo a CON_TENANT.',
                )
                self.assertTrue(motivo.strip(), f'{etiqueta} necesita un motivo escrito.')

    def test_ningun_modelo_auditado_quedo_sin_clasificar(self):
        """Tripwire: si aparece una llamada nueva a registrar_evento, hay que clasificarla.

        No deduce el modelo del código —`objeto=` suele ser una variable local, y el
        mismo nombre (`actividad`) apunta a dos modelos distintos según el archivo—, así
        que cuenta los sitios de llamada. Cambiar ese número obliga a pasar por acá y
        decidir si el modelo recién auditado llega al tenant o no.

        Es deliberadamente tonto: la alternativa (inferir tipos) fallaría en silencio,
        que es exactamente el problema que esta clase existe para evitar.
        """
        sitios = []
        for archivo in sorted(pathlib.Path(settings.BASE_DIR, 'apps').rglob('*.py')):
            if 'migrations' in archivo.parts or 'tests' in archivo.name:
                continue
            arbol = ast.parse(archivo.read_text(encoding='utf-8', errors='replace'))
            for nodo in ast.walk(arbol):
                if isinstance(nodo, ast.Call) and getattr(nodo.func, 'id', None) == 'registrar_evento':
                    sitios.append(f'{archivo.name}:{nodo.lineno}')

        self.assertEqual(
            len(sitios), 101,
            'Cambió la cantidad de llamadas a registrar_evento (eran 101 el '
            '1-oct-2026). Si agregaste una sobre un modelo que todavía no figura en '
            'CON_TENANT / COMPARTIDOS_LEGITIMOS / SIN_TENANT_POR_NOMBRE_DEL_CAMPO, '
            'clasificalo ahí y actualizá este número. Un modelo sin clasificar se '
            'audita sin tenant y queda visible para todas las unidades de negocio.',
        )

    def test_un_modelo_sin_ninguna_ruta_se_audita_sin_tenant(self):
        """La demostración del agujero, sobre un modelo real de la lista.

        `ReporteViatico` tiene un tenant único (su farmacia visitada pertenece a una
        sola unidad) y aun así el evento sale sin él. Sin esta prueba, el día que se
        decida arreglar la resolución no habría nada que dijera qué cambió.
        """
        from decimal import Decimal

        from django.utils import timezone

        from apps.activos.models import Colaborador
        from apps.viaticos.models import ReporteViatico, RubroViatico

        colaborador = Colaborador.objects.create(
            nombre='Técnico SG', cedula='0911111111', unidad_negocio=self.sg,
        )
        reporte = ReporteViatico.objects.create(
            colaborador=colaborador, farmacia_visitada=self.farmacia,
            fecha=timezone.localdate(), rubro=RubroViatico.ALIMENTACION, monto=Decimal('12.50'),
        )
        registrar_evento(usuario=self.usuario, accion='viatico.crear', objeto=reporte)

        evento = EventoAuditoria.objects.get(accion='viatico.crear')
        self.assertEqual(evento.modelo, 'viaticos.ReporteViatico')
        self.assertIsNone(
            evento.unidad_negocio,
            'Si esto ahora trae la unidad de negocio, se corrigió la resolución: mové '
            'ReporteViatico a CON_TENANT y actualizá esta prueba.',
        )
        # Y el tenant sí estaba disponible en los datos, por dos caminos distintos.
        self.assertEqual(reporte.farmacia_visitada.unidad_negocio, self.sg)
        self.assertEqual(reporte.colaborador.unidad_negocio, self.sg)


class EventoAuditoriaInmutableTests(TestCase):
    def test_no_se_puede_eliminar(self):
        usuario = User.objects.create_user(username='u', password='x')
        registrar_evento(usuario=usuario, accion='sistema.prueba')
        evento = EventoAuditoria.objects.get(accion='sistema.prueba')
        with self.assertRaises(NotImplementedError):
            evento.delete()
