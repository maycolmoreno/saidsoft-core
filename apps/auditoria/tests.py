from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase

from apps.catalogo.models import Farmacia, Grupo, UnidadNegocio

from .models import EventoAuditoria, registrar_evento


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


class EventoAuditoriaInmutableTests(TestCase):
    def test_no_se_puede_eliminar(self):
        usuario = User.objects.create_user(username='u', password='x')
        registrar_evento(usuario=usuario, accion='sistema.prueba')
        evento = EventoAuditoria.objects.get(accion='sistema.prueba')
        with self.assertRaises(NotImplementedError):
            evento.delete()
