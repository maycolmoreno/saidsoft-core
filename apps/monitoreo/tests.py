from datetime import timedelta

from django.contrib.auth.models import User
from django.core import mail
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.catalogo.models import Estacion, Farmacia, Grupo, UnidadNegocio
from apps.cuentas.models import PerfilUsuario

from .models import Alerta, Metrica, MuestraMetrica, ReglaAlerta
from .services import (
    evaluar_regla_bitlocker, evaluar_reglas_metricas, reglas_aplicables_a, resolver_alertas_bitlocker,
    resolver_alertas_sin_heartbeat,
)


class EvaluarReglasMetricasTests(TestCase):
    def setUp(self):
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg)
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            monitorear_recursos=True,
        )
        self.usuario = User.objects.create_user(username='u', password='x')
        self.regla = ReglaAlerta.objects.create(
            nombre='CPU alta', metrica=Metrica.CPU_CARGA_PCT, operador=ReglaAlerta.Operador.GTE,
            umbral=90, duracion_minutos=10, creado_por=self.usuario,
        )

    def _crear_muestra(self, cpu, hace_minutos=0):
        muestra = MuestraMetrica.objects.create(estacion=self.estacion, cpu_carga_pct=cpu)
        if hace_minutos:
            MuestraMetrica.objects.filter(pk=muestra.pk).update(
                timestamp=timezone.now() - timedelta(minutes=hace_minutos),
            )
            muestra.refresh_from_db()
        return muestra

    def test_pico_aislado_no_abre_alerta(self):
        # Sin historial previo (estación "nueva"): una sola muestra alta no alcanza
        # para confirmar que la condición se sostuvo duracion_minutos.
        muestra = self._crear_muestra(95)
        evaluar_reglas_metricas(self.estacion, muestra)
        self.assertFalse(Alerta.objects.exists())

    def test_condicion_sostenida_abre_alerta(self):
        self._crear_muestra(95, hace_minutos=15)  # más vieja que duracion_minutos=10
        muestra = self._crear_muestra(96)
        evaluar_reglas_metricas(self.estacion, muestra)

        alerta = Alerta.objects.get()
        self.assertEqual(alerta.estado, Alerta.Estado.ABIERTA)
        self.assertEqual(alerta.valor_disparador, 96)

    def test_no_duplica_alerta_ya_activa(self):
        self._crear_muestra(95, hace_minutos=15)
        evaluar_reglas_metricas(self.estacion, self._crear_muestra(96))
        evaluar_reglas_metricas(self.estacion, self._crear_muestra(97))
        self.assertEqual(Alerta.objects.count(), 1)

    def test_se_resuelve_sola_al_normalizarse(self):
        self._crear_muestra(95, hace_minutos=15)
        evaluar_reglas_metricas(self.estacion, self._crear_muestra(96))
        evaluar_reglas_metricas(self.estacion, self._crear_muestra(50))

        alerta = Alerta.objects.get()
        self.assertEqual(alerta.estado, Alerta.Estado.RESUELTA)
        self.assertIsNotNone(alerta.resuelta_en)

    def test_notifica_por_correo_al_abrir_no_al_sostenerla(self):
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.email = 'ops@example.com'
        self.usuario.save(update_fields=['email'])

        self._crear_muestra(95, hace_minutos=15)
        evaluar_reglas_metricas(self.estacion, self._crear_muestra(96))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('CPU alta', mail.outbox[0].subject)

        # Una segunda muestra que sigue incumpliendo no debe reenviar el correo.
        evaluar_reglas_metricas(self.estacion, self._crear_muestra(97))
        self.assertEqual(len(mail.outbox), 1)


class SinHeartbeatAlertaTests(TestCase):
    def setUp(self):
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg)
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            estado_conexion=Estacion.EstadoConexion.ONLINE,
            ultimo_heartbeat=timezone.now() - timedelta(minutes=20),
        )
        usuario = User.objects.create_user(username='u2', password='x')
        self.regla = ReglaAlerta.objects.create(
            nombre='Estación caída', metrica=Metrica.SIN_HEARTBEAT, umbral=10, creado_por=usuario,
        )

    def test_marcar_offline_abre_alerta_sin_heartbeat(self):
        call_command('marcar_estaciones_offline')

        self.estacion.refresh_from_db()
        self.assertEqual(self.estacion.estado_conexion, Estacion.EstadoConexion.OFFLINE)
        alerta = Alerta.objects.get()
        self.assertEqual(alerta.regla, self.regla)
        self.assertEqual(alerta.estado, Alerta.Estado.ABIERTA)

    def test_heartbeat_de_nuevo_resuelve_la_alerta(self):
        call_command('marcar_estaciones_offline')
        alerta = Alerta.objects.get()

        resolver_alertas_sin_heartbeat(self.estacion)

        alerta.refresh_from_db()
        self.assertEqual(alerta.estado, Alerta.Estado.RESUELTA)


class BitlockerAlertaTests(TestCase):
    """bitlocker_deshabilitado es binario (no serie de tiempo, ver docstring de
    ReglaAlerta): se abre/resuelve directo con cada reporte, sin duracion_minutos."""

    def setUp(self):
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg)
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            bitlocker_habilitado=False,
        )
        usuario = User.objects.create_user(username='u3', password='x')
        self.regla = ReglaAlerta.objects.create(
            nombre='Disco sin cifrar', metrica=Metrica.BITLOCKER_DESHABILITADO, umbral=0, creado_por=usuario,
        )

    def test_reportar_sin_cifrar_abre_alerta(self):
        evaluar_regla_bitlocker(self.estacion)
        alerta = Alerta.objects.get()
        self.assertEqual(alerta.regla, self.regla)
        self.assertEqual(alerta.estado, Alerta.Estado.ABIERTA)

    def test_no_duplica_si_ya_hay_una_activa(self):
        evaluar_regla_bitlocker(self.estacion)
        evaluar_regla_bitlocker(self.estacion)
        self.assertEqual(Alerta.objects.count(), 1)

    def test_reportar_cifrado_de_nuevo_resuelve_la_alerta(self):
        evaluar_regla_bitlocker(self.estacion)
        alerta = Alerta.objects.get()

        resolver_alertas_bitlocker(self.estacion)

        alerta.refresh_from_db()
        self.assertEqual(alerta.estado, Alerta.Estado.RESUELTA)
        self.assertIsNotNone(alerta.resuelta_en)

    def test_correo_no_menciona_umbral_numerico_sin_sentido(self):
        PerfilUsuario.objects.create(usuario=self.regla.creado_por, acceso_todas_unidades=True)
        self.regla.creado_por.email = 'ops@example.com'
        self.regla.creado_por.save(update_fields=['email'])

        evaluar_regla_bitlocker(self.estacion)

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('BitLocker deshabilitado', mail.outbox[0].body)
        self.assertNotIn('umbral', mail.outbox[0].body.lower())


class ReglasAplicablesMultiTenantTests(TestCase):
    """Una regla privada de un cliente no debe aplicar a estaciones de otro —
    mismo espíritu que los tests de fuga de R1 (apps.catalogo.tests)."""

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        usuario = User.objects.create_user(username='creador', password='x')
        self.regla_sg = ReglaAlerta.objects.create(
            nombre='Privada SG', metrica=Metrica.CPU_CARGA_PCT, umbral=90,
            unidad_negocio=self.sg, creado_por=usuario,
        )
        self.regla_global = ReglaAlerta.objects.create(
            nombre='Global', metrica=Metrica.CPU_CARGA_PCT, umbral=95, creado_por=usuario,
        )

    def test_regla_privada_no_aplica_a_otro_tenant(self):
        aplicables_mia = reglas_aplicables_a(self.mia)
        self.assertNotIn(self.regla_sg, aplicables_mia)
        self.assertIn(self.regla_global, aplicables_mia)

    def test_regla_privada_aplica_a_su_propio_tenant(self):
        aplicables_sg = reglas_aplicables_a(self.sg)
        self.assertIn(self.regla_sg, aplicables_sg)
        self.assertIn(self.regla_global, aplicables_sg)


class PurgarMetricasTaskTests(TestCase):
    """CELERY_TASK_ALWAYS_EAGER=True hace que .delay() corra sincrónico en el test."""

    def test_delay_borra_muestras_viejas(self):
        from apps.monitoreo.tasks import purgar_metricas_task

        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg)
        estacion = Estacion.objects.create(codigo='ML001-A', farmacia=farmacia)
        vieja = MuestraMetrica.objects.create(estacion=estacion, cpu_carga_pct=50)
        MuestraMetrica.objects.filter(pk=vieja.pk).update(timestamp=timezone.now() - timedelta(days=40))
        reciente = MuestraMetrica.objects.create(estacion=estacion, cpu_carga_pct=60)

        resultado = purgar_metricas_task.delay()

        self.assertFalse(MuestraMetrica.objects.filter(pk=vieja.pk).exists())
        self.assertTrue(MuestraMetrica.objects.filter(pk=reciente.pk).exists())
        self.assertIn('1 muestra', resultado.get())
