"""Seguimiento en vivo de despliegues e instalaciones (apps.panel.seguimiento)."""
from django.contrib.auth.models import Permission
from django.urls import reverse

from apps.catalogo.models import OrdenAgente
from apps.catalogo.tests_ordenes import BaseCola
from apps.cuentas.models import PerfilUsuario
from apps.despliegues.models import Despliegue, ResultadoDespliegue
from apps.despliegues.services import porcentaje_error, publicar_despliegue
from apps.panel import seguimiento
from apps.software.services import publicar_solicitud

E = OrdenAgente.Estado


class RastreadorTests(BaseCola):
    def _filas(self, despliegue):
        return seguimiento.rastreador(
            seguimiento.resultados_despliegue(despliegue), seguimiento.DESPLIEGUE,
            origen_id=despliegue.pk, modo_aplicacion=despliegue.modo_aplicacion,
        )

    def test_pasos_categorias_y_orden_de_prioridad(self):
        despliegue = self._despliegue(self.a, self.b)
        publicar_despliegue(despliegue)
        self._rep_despliegue(self.a, despliegue, 'recibido')
        self._rep_despliegue(self.a, despliegue, 'descargado')
        OrdenAgente.objects.filter(estacion=self.b).update(estado=E.INTERRUMPIDA)

        filas = self._filas(despliegue)
        self.assertEqual([f['estacion'].codigo for f in filas], ['ML001-B', 'ML001-A'])  # la incierta primero
        incierta, en_curso = filas
        self.assertEqual((incierta['categoria'], incierta['texto']), (seguimiento.INCIERTO, 'Interrumpida'))
        self.assertIsNotNone(incierta['orden'])
        self.assertEqual((en_curso['categoria'], en_curso['texto']), (seguimiento.CURSO, 'Descargada'))
        self.assertEqual([p['tono'] for p in en_curso['pasos']], ['good', 'good', 'good', 'accent', 'neutral'])

    def test_lista_y_error_en_el_paso_en_que_fallo(self):
        despliegue = self._despliegue(self.a, self.b)
        publicar_despliegue(despliegue)
        for paso in ('recibido', 'descargado', 'pos_cerrado', 'ok'):
            self._rep_despliegue(self.a, despliegue, paso, version_nueva='4.3.0')
        self._rep_despliegue(self.b, despliegue, 'recibido')
        self._rep_despliegue(self.b, despliegue, 'error', detalle='No se pudo cerrar el POS')
        por_codigo = {f['estacion'].codigo: f for f in self._filas(despliegue)}
        self.assertEqual(por_codigo['ML001-A']['categoria'], seguimiento.LISTO)
        self.assertEqual({p['tono'] for p in por_codigo['ML001-A']['pasos']}, {'good'})
        error = por_codigo['ML001-B']
        self.assertEqual(error['categoria'], seguimiento.ERROR)
        self.assertEqual([p['tono'] for p in error['pasos']], ['good', 'good', 'critical', 'neutral', 'neutral'])

    def test_esperando_cierre_de_caja(self):
        despliegue = self._despliegue(self.a)
        Despliegue.objects.filter(pk=despliegue.pk).update(modo_aplicacion=Despliegue.ModoAplicacion.CIERRE_POS)
        despliegue.refresh_from_db()
        publicar_despliegue(despliegue)
        self._rep_despliegue(self.a, despliegue, 'recibido')
        self._rep_despliegue(self.a, despliegue, 'hash_verificado')
        self.assertEqual(self._filas(despliegue)[0]['texto'], 'Esperando cierre de caja')

    def test_conteos_para_la_barra(self):
        filas = [{'categoria': c} for c in ('listo', 'listo', 'curso', 'incierto')]
        self.assertEqual(seguimiento.conteos(filas), [
            ('listo', 'Listas', 2, 50.0), ('curso', 'En curso', 1, 25.0), ('incierto', 'Inciertas', 1, 25.0),
        ])

    def test_anillos_de_la_cadena(self):
        primero = self._despliegue(self.a)
        publicar_despliegue(primero)
        segundo = self._despliegue(self.b)
        Despliegue.objects.filter(pk=segundo.pk).update(despliegue_origen=primero)
        segundo.refresh_from_db()
        desde_el_segundo = seguimiento.anillos(segundo)
        self.assertEqual([(a['numero'], a['actual']) for a in desde_el_segundo], [(1, False), (2, True)])
        self.assertEqual(desde_el_segundo[0]['total'], 1)

    def test_porcentaje_de_error_igual_al_del_freno(self):
        despliegue = self._despliegue(self.a, self.b)
        publicar_despliegue(despliegue)
        self.assertIsNone(porcentaje_error(despliegue))  # nadie reportó todavía
        ResultadoDespliegue.objects.filter(despliegue=despliegue, estacion=self.a).update(estado='error')
        ResultadoDespliegue.objects.filter(despliegue=despliegue, estacion=self.b).update(estado='aplicando')
        self.assertEqual(porcentaje_error(despliegue), 50.0)


class VistasDeSeguimientoTests(BaseCola):
    def _entrar(self, *codenames):
        PerfilUsuario.objects.get_or_create(usuario=self.usuario, defaults={'acceso_todas_unidades': True})
        self.usuario.user_permissions.add(*Permission.objects.filter(codename__in=codenames))
        self.client.force_login(self.usuario)

    def test_despliegue_muestra_rastreador_freno_y_resolver_solo_con_permiso(self):
        despliegue = self._despliegue(self.a, self.b)
        publicar_despliegue(despliegue)
        OrdenAgente.objects.filter(estacion=self.b).update(estado=E.INTERRUMPIDA)
        url = reverse('panel:despliegue_progreso_partial', args=[despliegue.pk])

        self._entrar('view_despliegue')
        resp = self.client.get(url)
        self.assertContains(resp, 'Rastreador por estación')
        self.assertContains(resp, 'Freno automático')
        self.assertContains(resp, 'todavía no reportó ninguna estación')
        self.assertNotContains(resp, '>Resolver<')

        self.usuario.user_permissions.add(Permission.objects.get(codename='resolver_ordenagente'))
        self.client.force_login(self.usuario)
        orden = OrdenAgente.objects.get(estacion=self.b)
        self.assertContains(self.client.get(url), reverse('admin:catalogo_ordenagente_resolver', args=[orden.pk]))

    def test_instalacion_muestra_rastreador(self):
        solicitud = self._solicitud(self.a)
        publicar_solicitud(solicitud)
        self._rep_software(self.a, solicitud, 'recibido')
        self._entrar('view_solicitudinstalacion')
        resp = self.client.get(reverse('panel:solicitud_instalacion_progreso_partial', args=[solicitud.pk]))
        self.assertContains(resp, 'Rastreador por estación')
        self.assertContains(resp, 'Recibida')
        self.assertNotContains(resp, 'Freno automático')
