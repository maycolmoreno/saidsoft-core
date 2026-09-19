import io
import json
import urllib.error
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import websocket
from django.contrib.auth.models import Permission, User
from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.catalogo.models import Estacion, Farmacia, Grupo, UnidadNegocio
from apps.activos.models import Activo
from apps.cuentas.models import PerfilUsuario
from apps.mantenimiento.models import Mantenimiento, PrioridadMantenimiento, TipoOrigenMantenimiento
from apps.monitoreo.adapters.meshcentral import AdaptadorMeshCentral, _en_linea, _id_corto

from .mikrotik import sincronizar_ancho_banda_farmacias
from .models import (
    Alerta, CanalNotificacion, EstadoDispositivo, EventoMonitoreo, Metrica, MuestraMetrica, MuestraRedFarmacia,
    PosErrorDetectado, ReglaAlerta, VentanaMantenimiento,
)
from .services import (
    UMBRAL_ESCALAMIENTO_MINUTOS, abrir_o_mantener_alerta, clasificar_error_pos, escalar_alertas_abiertas,
    evaluar_cruce_monitoreo, resolver_condicion,
    evaluar_regla_bitlocker, evaluar_regla_pos_errores, evaluar_reglas_metricas, notificar_alerta,
    registrar_estado_dispositivo, reglas_aplicables_a, resolver_alertas_agente_caido_red_viva,
    resolver_alertas_bitlocker, resolver_alertas_sin_heartbeat,
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

    def _crear_muestra_disco(self, disco_usado_pct, hace_minutos=0):
        # disco_usado_pct es una property (100 - libre/total), no un campo — se arma
        # total/libre para que dé el porcentaje pedido.
        muestra = MuestraMetrica.objects.create(
            estacion=self.estacion, disco_total_gb=100.0, disco_libre_gb=100.0 - disco_usado_pct,
        )
        if hace_minutos:
            MuestraMetrica.objects.filter(pk=muestra.pk).update(
                timestamp=timezone.now() - timedelta(minutes=hace_minutos),
            )
            muestra.refresh_from_db()
        return muestra

    def test_regla_de_disco_usado_abre_alerta_por_el_mismo_mecanismo_generico(self):
        # evaluar_reglas_metricas lee getattr(muestra, regla.metrica) — no hace falta
        # tocar el service al agregar una métrica nueva, esta prueba lo confirma.
        regla_disco = ReglaAlerta.objects.create(
            nombre='Disco lleno', metrica=Metrica.DISCO_USADO_PCT, operador=ReglaAlerta.Operador.GTE,
            umbral=90, duracion_minutos=10, creado_por=self.usuario,
        )
        self._crear_muestra_disco(95, hace_minutos=15)
        muestra = self._crear_muestra_disco(96)
        evaluar_reglas_metricas(self.estacion, muestra)

        alerta = Alerta.objects.get(regla=regla_disco)
        self.assertEqual(alerta.estado, Alerta.Estado.ABIERTA)
        self.assertEqual(alerta.valor_disparador, 96)

    def test_regla_de_red_abre_alerta_por_el_mismo_mecanismo_generico(self):
        # Mismo mecanismo genérico que la prueba de disco arriba — red_total_kbps es
        # una property, no una columna, y también funciona vía getattr sin cambios.
        regla_red = ReglaAlerta.objects.create(
            nombre='Consumo de red alto', metrica=Metrica.RED_TOTAL_KBPS, operador=ReglaAlerta.Operador.GTE,
            umbral=5000, duracion_minutos=10, creado_por=self.usuario,
        )
        vieja = MuestraMetrica.objects.create(
            estacion=self.estacion, red_recibido_kbps=4000, red_enviado_kbps=1200,
        )
        MuestraMetrica.objects.filter(pk=vieja.pk).update(timestamp=timezone.now() - timedelta(minutes=15))
        muestra = MuestraMetrica.objects.create(
            estacion=self.estacion, red_recibido_kbps=4500, red_enviado_kbps=1300,
        )
        evaluar_reglas_metricas(self.estacion, muestra)

        alerta = Alerta.objects.get(regla=regla_red)
        self.assertEqual(alerta.estado, Alerta.Estado.ABIERTA)
        self.assertEqual(alerta.valor_disparador, 5800)

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

    def test_marcar_offline_registra_estado_dispositivo_mqtt(self):
        call_command('marcar_estaciones_offline')
        estado = EstadoDispositivo.objects.get(estacion=self.estacion, fuente=EstadoDispositivo.Fuente.MQTT)
        self.assertFalse(estado.en_linea)


class RegistrarEstadoDispositivoTests(TestCase):
    def setUp(self):
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg)
        self.estacion = Estacion.objects.create(codigo='ML001-A', farmacia=farmacia)

    def test_primera_senal_crea_snapshot_y_evento(self):
        registrar_estado_dispositivo(self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL, en_linea=True)
        estado = EstadoDispositivo.objects.get(estacion=self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL)
        self.assertTrue(estado.en_linea)
        self.assertEqual(EventoMonitoreo.objects.filter(estacion=self.estacion).count(), 1)

    def test_senal_repetida_no_duplica_snapshot_ni_evento(self):
        registrar_estado_dispositivo(self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL, en_linea=True)
        registrar_estado_dispositivo(self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL, en_linea=True)
        self.assertEqual(EstadoDispositivo.objects.filter(estacion=self.estacion).count(), 1)
        self.assertEqual(EventoMonitoreo.objects.filter(estacion=self.estacion).count(), 1)

    def test_transicion_agrega_nuevo_evento(self):
        registrar_estado_dispositivo(self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL, en_linea=True)
        registrar_estado_dispositivo(self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL, en_linea=False)
        self.assertEqual(EventoMonitoreo.objects.filter(estacion=self.estacion).count(), 2)
        estado = EstadoDispositivo.objects.get(estacion=self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL)
        self.assertFalse(estado.en_linea)

    def test_dos_fuentes_de_la_misma_estacion_no_se_pisan(self):
        registrar_estado_dispositivo(self.estacion, fuente=EstadoDispositivo.Fuente.MQTT, en_linea=False)
        registrar_estado_dispositivo(self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL, en_linea=True)
        self.assertEqual(EstadoDispositivo.objects.filter(estacion=self.estacion).count(), 2)
        self.assertFalse(
            EstadoDispositivo.objects.get(estacion=self.estacion, fuente=EstadoDispositivo.Fuente.MQTT).en_linea,
        )
        self.assertTrue(
            EstadoDispositivo.objects.get(
                estacion=self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL,
            ).en_linea,
        )


class CruceMonitoreoTests(TestCase):
    """agente_caido_red_viva: MQTT sin heartbeat + MeshCentral todavía en línea."""

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=self.sg)
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            estado_conexion=Estacion.EstadoConexion.OFFLINE,
            ultimo_heartbeat=timezone.now() - timedelta(minutes=20),
        )
        usuario = User.objects.create_user(username='u4', password='x')
        self.regla = ReglaAlerta.objects.create(
            nombre='Agente caído, red viva', metrica=Metrica.AGENTE_CAIDO_RED_VIVA, umbral=10, creado_por=usuario,
        )

    def test_abre_alerta_cuando_meshcentral_ve_online_y_mqtt_no(self):
        registrar_estado_dispositivo(self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL, en_linea=True)
        abiertas = evaluar_cruce_monitoreo()
        self.assertEqual(abiertas, 1)
        alerta = Alerta.objects.get()
        self.assertEqual(alerta.regla, self.regla)
        self.assertEqual(alerta.estado, Alerta.Estado.ABIERTA)

    def test_no_abre_si_meshcentral_tambien_esta_offline(self):
        registrar_estado_dispositivo(self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL, en_linea=False)
        self.assertEqual(evaluar_cruce_monitoreo(), 0)
        self.assertFalse(Alerta.objects.exists())

    def test_no_abre_si_estacion_mqtt_sigue_online(self):
        self.estacion.estado_conexion = Estacion.EstadoConexion.ONLINE
        self.estacion.save(update_fields=['estado_conexion'])
        registrar_estado_dispositivo(self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL, en_linea=True)
        self.assertEqual(evaluar_cruce_monitoreo(), 0)
        self.assertFalse(Alerta.objects.exists())

    def test_no_abre_bajo_el_umbral_de_minutos(self):
        self.estacion.ultimo_heartbeat = timezone.now() - timedelta(minutes=5)  # umbral es 10
        self.estacion.save(update_fields=['ultimo_heartbeat'])
        registrar_estado_dispositivo(self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL, en_linea=True)
        self.assertEqual(evaluar_cruce_monitoreo(), 0)
        self.assertFalse(Alerta.objects.exists())

    def test_dato_de_meshcentral_viejo_no_cuenta(self):
        registrar_estado_dispositivo(self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL, en_linea=True)
        EstadoDispositivo.objects.filter(estacion=self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL).update(
            actualizado_en=timezone.now() - timedelta(minutes=45),  # más viejo que FRESCURA_MESHCENTRAL_MINUTOS
        )
        self.assertEqual(evaluar_cruce_monitoreo(), 0)
        self.assertFalse(Alerta.objects.exists())

    def test_no_duplica_alerta_ya_activa(self):
        registrar_estado_dispositivo(self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL, en_linea=True)
        evaluar_cruce_monitoreo()
        evaluar_cruce_monitoreo()
        self.assertEqual(Alerta.objects.count(), 1)

    def test_heartbeat_mqtt_de_nuevo_resuelve_la_alerta(self):
        registrar_estado_dispositivo(self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL, en_linea=True)
        evaluar_cruce_monitoreo()
        alerta = Alerta.objects.get()

        resolver_alertas_agente_caido_red_viva(self.estacion)

        alerta.refresh_from_db()
        self.assertEqual(alerta.estado, Alerta.Estado.RESUELTA)

    def test_regla_privada_de_otro_cliente_no_aplica(self):
        mia = UnidadNegocio.objects.get(codigo='MIA')
        self.regla.unidad_negocio = mia
        self.regla.save(update_fields=['unidad_negocio'])

        registrar_estado_dispositivo(self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL, en_linea=True)
        self.assertEqual(evaluar_cruce_monitoreo(), 0)
        self.assertFalse(Alerta.objects.exists())


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


class EvaluarReglaPosErroresTests(TestCase):
    """pos_errores: cada reporte ya es una ventana cerrada, sin duracion_minutos (como
    bitlocker), pero sí reusa umbral/operador de ReglaAlerta (a diferencia de
    bitlocker, que es puramente binario)."""

    def setUp(self):
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg)
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        usuario = User.objects.create_user(username='u_pos_err', password='x')
        self.regla = ReglaAlerta.objects.create(
            nombre='Errores del POS', metrica=Metrica.POS_ERRORES,
            operador=ReglaAlerta.Operador.GTE, umbral=1, creado_por=usuario,
        )

    def test_por_debajo_del_umbral_no_abre_nada(self):
        evaluar_regla_pos_errores(self.estacion, 0)
        self.assertFalse(Alerta.objects.exists())

    def test_alcanzar_el_umbral_abre_alerta_con_el_total_como_valor(self):
        evaluar_regla_pos_errores(self.estacion, 3)
        alerta = Alerta.objects.get()
        self.assertEqual(alerta.regla, self.regla)
        self.assertEqual(alerta.estado, Alerta.Estado.ABIERTA)
        self.assertEqual(alerta.valor_disparador, 3)

    def test_no_duplica_si_ya_hay_una_activa(self):
        evaluar_regla_pos_errores(self.estacion, 2)
        evaluar_regla_pos_errores(self.estacion, 5)
        self.assertEqual(Alerta.objects.count(), 1)

    def test_una_ventana_limpia_resuelve_la_alerta(self):
        evaluar_regla_pos_errores(self.estacion, 2)
        evaluar_regla_pos_errores(self.estacion, 0)
        alerta = Alerta.objects.get()
        self.assertEqual(alerta.estado, Alerta.Estado.RESUELTA)
        self.assertIsNotNone(alerta.resuelta_en)


class VentanaMantenimientoHookTests(TestCase):
    """abrir_o_mantener_alerta consulta ventana_mantenimiento_activa antes que nada —
    un solo hook cubre las rutas de evaluación existentes (métricas, bitlocker,
    pos_errores) sin tocar cada evaluador por separado."""

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=self.sg)
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=self.farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            bitlocker_habilitado=False,
        )
        self.usuario = User.objects.create_user(username='u_vm', password='x')

    def _crear_ventana(
        self, *, destino_tipo=VentanaMantenimiento.DestinoTipo.CADENA, estaciones=None,
        activo=True, hace_minutos=30, dura_minutos=60,
    ):
        ventana = VentanaMantenimiento.objects.create(
            unidad_negocio=self.sg, destino_tipo=destino_tipo, activo=activo,
            desde=timezone.now() - timedelta(minutes=hace_minutos),
            hasta=timezone.now() + timedelta(minutes=dura_minutos),
            motivo='Despliegue de POS v5.2', creado_por=self.usuario,
        )
        if estaciones:
            ventana.estaciones.set(estaciones)
        return ventana

    def test_silencia_alerta_de_bitlocker(self):
        ReglaAlerta.objects.create(
            nombre='Disco sin cifrar', metrica=Metrica.BITLOCKER_DESHABILITADO, umbral=0, creado_por=self.usuario,
        )
        self._crear_ventana()
        evaluar_regla_bitlocker(self.estacion)
        self.assertFalse(Alerta.objects.exists())

    def test_silencia_alerta_de_pos_errores(self):
        ReglaAlerta.objects.create(
            nombre='Errores del POS', metrica=Metrica.POS_ERRORES, operador=ReglaAlerta.Operador.GTE,
            umbral=1, creado_por=self.usuario,
        )
        self._crear_ventana()
        evaluar_regla_pos_errores(self.estacion, 5)
        self.assertFalse(Alerta.objects.exists())

    def test_silencia_alerta_de_metricas(self):
        ReglaAlerta.objects.create(
            nombre='CPU alta', metrica=Metrica.CPU_CARGA_PCT, operador=ReglaAlerta.Operador.GTE,
            umbral=90, duracion_minutos=10, creado_por=self.usuario,
        )
        vieja = MuestraMetrica.objects.create(estacion=self.estacion, cpu_carga_pct=95)
        MuestraMetrica.objects.filter(pk=vieja.pk).update(timestamp=timezone.now() - timedelta(minutes=15))
        self._crear_ventana()
        muestra = MuestraMetrica.objects.create(estacion=self.estacion, cpu_carga_pct=96)
        evaluar_reglas_metricas(self.estacion, muestra)
        self.assertFalse(Alerta.objects.exists())

    def test_ventana_ya_terminada_no_silencia(self):
        ReglaAlerta.objects.create(
            nombre='Disco sin cifrar', metrica=Metrica.BITLOCKER_DESHABILITADO, umbral=0, creado_por=self.usuario,
        )
        self._crear_ventana(hace_minutos=120, dura_minutos=-60)  # terminó hace una hora
        evaluar_regla_bitlocker(self.estacion)
        self.assertTrue(Alerta.objects.exists())

    def test_ventana_inactiva_no_silencia(self):
        ReglaAlerta.objects.create(
            nombre='Disco sin cifrar', metrica=Metrica.BITLOCKER_DESHABILITADO, umbral=0, creado_por=self.usuario,
        )
        self._crear_ventana(activo=False)
        evaluar_regla_bitlocker(self.estacion)
        self.assertTrue(Alerta.objects.exists())

    def test_destino_de_estaciones_puntuales_no_cubre_una_estacion_distinta(self):
        otra = Estacion.objects.create(
            codigo='ML001-B', farmacia=self.farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            bitlocker_habilitado=False,
        )
        ReglaAlerta.objects.create(
            nombre='Disco sin cifrar', metrica=Metrica.BITLOCKER_DESHABILITADO, umbral=0, creado_por=self.usuario,
        )
        self._crear_ventana(destino_tipo=VentanaMantenimiento.DestinoTipo.ESTACIONES, estaciones=[otra])
        evaluar_regla_bitlocker(self.estacion)  # self.estacion no está en el destino de la ventana
        self.assertTrue(Alerta.objects.exists())

    def test_destino_de_estaciones_puntuales_si_cubre_la_estacion_elegida(self):
        ReglaAlerta.objects.create(
            nombre='Disco sin cifrar', metrica=Metrica.BITLOCKER_DESHABILITADO, umbral=0, creado_por=self.usuario,
        )
        self._crear_ventana(destino_tipo=VentanaMantenimiento.DestinoTipo.ESTACIONES, estaciones=[self.estacion])
        evaluar_regla_bitlocker(self.estacion)
        self.assertFalse(Alerta.objects.exists())


class NotificarAlertaWebhookTeamsTests(TestCase):
    """notificar_alerta reenvía por Teams además de correo — canal global, canal
    propio de la unidad de negocio, o ninguno (M3)."""

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=self.sg)
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        usuario = User.objects.create_user(username='u_teams', password='x')
        self.regla = ReglaAlerta.objects.create(
            nombre='CPU alta', metrica=Metrica.CPU_CARGA_PCT, umbral=90, creado_por=usuario,
        )
        self.alerta = Alerta.objects.create(regla=self.regla, estacion=self.estacion, valor_disparador=95)

    def test_sin_canal_configurado_no_llama_al_webhook(self):
        with patch('apps.monitoreo.services.urllib.request.urlopen') as urlopen:
            notificar_alerta(self.alerta)
        urlopen.assert_not_called()

    def test_canal_global_recibe_el_webhook(self):
        CanalNotificacion.objects.create(
            destino='https://outlook.office.com/webhook/global', creado_por=self.regla.creado_por,
        )
        with patch('apps.monitoreo.services.urllib.request.urlopen') as urlopen:
            notificar_alerta(self.alerta)
        urlopen.assert_called_once()

    def test_canal_de_otra_unidad_de_negocio_no_recibe_nada(self):
        CanalNotificacion.objects.create(
            unidad_negocio=self.mia, destino='https://outlook.office.com/webhook/mia',
            creado_por=self.regla.creado_por,
        )
        with patch('apps.monitoreo.services.urllib.request.urlopen') as urlopen:
            notificar_alerta(self.alerta)
        urlopen.assert_not_called()

    def test_canal_inactivo_no_recibe_nada(self):
        CanalNotificacion.objects.create(
            unidad_negocio=self.sg, destino='https://outlook.office.com/webhook/sg', activo=False,
            creado_por=self.regla.creado_por,
        )
        with patch('apps.monitoreo.services.urllib.request.urlopen') as urlopen:
            notificar_alerta(self.alerta)
        urlopen.assert_not_called()

    def test_webhook_caido_no_rompe_la_notificacion(self):
        CanalNotificacion.objects.create(
            unidad_negocio=self.sg, destino='https://outlook.office.com/webhook/sg', creado_por=self.regla.creado_por,
        )
        with patch('apps.monitoreo.services.urllib.request.urlopen', side_effect=urllib.error.URLError('caído')):
            notificar_alerta(self.alerta)  # no debe lanzar

    def test_escalamiento_marca_el_asunto_como_sin_atender(self):
        PerfilUsuario.objects.create(usuario=self.regla.creado_por, acceso_todas_unidades=True)
        self.regla.creado_por.email = 'ops@example.com'
        self.regla.creado_por.save(update_fields=['email'])

        notificar_alerta(self.alerta, escalamiento=True)
        self.assertIn('SIN ATENDER', mail.outbox[-1].subject)


class EscalarAlertasAbiertasTests(TestCase):
    """Reenvía la notificación de una Alerta ABIERTA que nadie reconoció a tiempo —
    una sola vez por alerta (escalada_en), y solo mientras siga ABIERTA (M3)."""

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=self.sg)
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self.usuario = User.objects.create_user(username='u_escalar', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.email = 'ops@example.com'
        self.usuario.save(update_fields=['email'])
        self.regla = ReglaAlerta.objects.create(
            nombre='CPU alta', metrica=Metrica.CPU_CARGA_PCT, umbral=90, creado_por=self.usuario,
        )

    def _crear_alerta(self, *, estado=Alerta.Estado.ABIERTA, hace_minutos=0):
        alerta = Alerta.objects.create(
            regla=self.regla, estacion=self.estacion, valor_disparador=95, estado=estado,
        )
        if hace_minutos:
            Alerta.objects.filter(pk=alerta.pk).update(
                abierta_en=timezone.now() - timedelta(minutes=hace_minutos),
            )
            alerta.refresh_from_db()
        return alerta

    def test_alerta_reciente_no_escala_todavia(self):
        self._crear_alerta(hace_minutos=UMBRAL_ESCALAMIENTO_MINUTOS - 5)
        escaladas = escalar_alertas_abiertas()
        self.assertEqual(escaladas, 0)

    def test_alerta_vieja_sin_reconocer_escala(self):
        alerta = self._crear_alerta(hace_minutos=UMBRAL_ESCALAMIENTO_MINUTOS + 5)
        escaladas = escalar_alertas_abiertas()
        self.assertEqual(escaladas, 1)
        alerta.refresh_from_db()
        self.assertIsNotNone(alerta.escalada_en)
        self.assertIn('SIN ATENDER', mail.outbox[-1].subject)

    def test_no_reescala_una_alerta_ya_escalada(self):
        self._crear_alerta(hace_minutos=UMBRAL_ESCALAMIENTO_MINUTOS + 5)
        escalar_alertas_abiertas()
        escaladas_de_nuevo = escalar_alertas_abiertas()
        self.assertEqual(escaladas_de_nuevo, 0)

    def test_alerta_reconocida_no_escala(self):
        self._crear_alerta(estado=Alerta.Estado.RECONOCIDA, hace_minutos=UMBRAL_ESCALAMIENTO_MINUTOS + 5)
        escaladas = escalar_alertas_abiertas()
        self.assertEqual(escaladas, 0)

    def test_alerta_resuelta_no_escala(self):
        self._crear_alerta(estado=Alerta.Estado.RESUELTA, hace_minutos=UMBRAL_ESCALAMIENTO_MINUTOS + 5)
        escaladas = escalar_alertas_abiertas()
        self.assertEqual(escaladas, 0)


class ClasificarErrorPosTests(TestCase):
    def test_venta_sin_lote_es_negocio(self):
        mensaje = 'VENTA SIN LOTE: 056-020-000105005 Usuario: jtorresq Defaul Code: 03317'
        self.assertEqual(clasificar_error_pos(mensaje), PosErrorDetectado.Categoria.NEGOCIO)

    def test_error_de_conexion_es_sistema(self):
        mensaje = 'Exception while reading from stream'
        self.assertEqual(clasificar_error_pos(mensaje), PosErrorDetectado.Categoria.SISTEMA)

    def test_mensaje_desconocido_por_defecto_es_sistema(self):
        # Ante la duda, un mensaje nuevo no reconocido se trata como señal real, no se
        # descarta en silencio — mismo criterio conservador que el resto del proyecto.
        self.assertEqual(clasificar_error_pos('un error nunca antes visto'), PosErrorDetectado.Categoria.SISTEMA)


class PosErrorDetectadoModeloTests(TestCase):
    def test_unique_together_estacion_mensaje(self):
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg)
        estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        from django.db import IntegrityError, transaction

        PosErrorDetectado.objects.create(estacion=estacion, mensaje='no existe la relación X', cantidad_total=1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            PosErrorDetectado.objects.create(estacion=estacion, mensaje='no existe la relación X', cantidad_total=1)


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


# `CELERY_TASK_ALWAYS_EAGER` solo está puesto en config/settings/desarrollo.py, y no
# se lee del entorno. Sin este override la prueba pasa en local y falla SIEMPRE dentro
# del contenedor — que es justo donde CLAUDE.md pide correr la suite para probar contra
# PostgreSQL. Una prueba no debe depender de qué módulo de settings esté cargado.
@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
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


# `CELERY_TASK_ALWAYS_EAGER` solo está puesto en config/settings/desarrollo.py, y no
# se lee del entorno. Sin este override la prueba pasa en local y falla SIEMPRE dentro
# del contenedor — que es justo donde CLAUDE.md pide correr la suite para probar contra
# PostgreSQL. Una prueba no debe depender de qué módulo de settings esté cargado.
@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
class PurgarEventosMonitoreoTaskTests(TestCase):
    def test_delay_borra_eventos_viejos(self):
        from apps.monitoreo.tasks import purgar_eventos_monitoreo_task

        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg)
        estacion = Estacion.objects.create(codigo='ML001-A', farmacia=farmacia)
        viejo = EventoMonitoreo.objects.create(estacion=estacion, fuente=EstadoDispositivo.Fuente.MQTT, en_linea=True)
        EventoMonitoreo.objects.filter(pk=viejo.pk).update(timestamp=timezone.now() - timedelta(days=40))
        reciente = EventoMonitoreo.objects.create(
            estacion=estacion, fuente=EstadoDispositivo.Fuente.MQTT, en_linea=False,
        )

        resultado = purgar_eventos_monitoreo_task.delay()

        self.assertFalse(EventoMonitoreo.objects.filter(pk=viejo.pk).exists())
        self.assertTrue(EventoMonitoreo.objects.filter(pk=reciente.pk).exists())
        self.assertIn('1 evento', resultado.get())


# `CELERY_TASK_ALWAYS_EAGER` solo está puesto en config/settings/desarrollo.py, y no
# se lee del entorno. Sin este override la prueba pasa en local y falla SIEMPRE dentro
# del contenedor — que es justo donde CLAUDE.md pide correr la suite para probar contra
# PostgreSQL. Una prueba no debe depender de qué módulo de settings esté cargado.
@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
class EvaluarCruceMonitoreoTaskTests(TestCase):
    def test_delay_abre_alertas_del_cruce(self):
        from apps.monitoreo.tasks import evaluar_cruce_monitoreo_task

        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg)
        estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            estado_conexion=Estacion.EstadoConexion.OFFLINE, ultimo_heartbeat=timezone.now() - timedelta(minutes=20),
        )
        usuario = User.objects.create_user(username='u5', password='x')
        ReglaAlerta.objects.create(
            nombre='Agente caído, red viva', metrica=Metrica.AGENTE_CAIDO_RED_VIVA, umbral=10, creado_por=usuario,
        )
        registrar_estado_dispositivo(estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL, en_linea=True)

        resultado = evaluar_cruce_monitoreo_task.delay()

        self.assertEqual(Alerta.objects.count(), 1)
        self.assertIn('1 alerta', resultado.get())


class AdaptadorMeshCentralTests(TestCase):
    """Solo la lógica pura (parseo/matching) — nada de esto abre un WebSocket real,
    ver apps.monitoreo.adapters.meshcentral para el protocolo verificado contra el
    código fuente del servidor."""

    def setUp(self):
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg)
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, meshcentral_node_id='abc123',
        )
        self.adaptador = AdaptadorMeshCentral()

    def test_id_corto_extrae_el_ultimo_segmento(self):
        self.assertEqual(_id_corto('node/domain0/abc123'), 'abc123')
        self.assertEqual(_id_corto('abc123'), 'abc123')
        self.assertEqual(_id_corto(''), '')

    def test_en_linea_es_el_bit_del_agente_meshagent(self):
        self.assertTrue(_en_linea(1))  # solo agente
        self.assertTrue(_en_linea(3))  # agente + CIRA/relay
        self.assertFalse(_en_linea(0))  # nada conectado
        self.assertFalse(_en_linea(2))  # solo CIRA/relay, sin agente — no cuenta como "en línea"

    def test_registrar_nodo_vincula_por_id_corto(self):
        procesados = self.adaptador._registrar_nodo('node/domain0/abc123', 1)
        self.assertEqual(procesados, 1)
        estado = EstadoDispositivo.objects.get(estacion=self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL)
        self.assertTrue(estado.en_linea)
        self.assertEqual(estado.detalle, {'conn': 1})

    def test_registrar_nodo_sin_estacion_vinculada_no_hace_nada(self):
        procesados = self.adaptador._registrar_nodo('node/domain0/desconocido', 1)
        self.assertEqual(procesados, 0)
        self.assertFalse(EstadoDispositivo.objects.exists())

    def test_registrar_nodo_vincula_automaticamente_por_nombre(self):
        # generar_comando_instalacion_meshcentral instala con --agentName=<código> —
        # si el nombre del nodo coincide con una estación sin vincular, se enlaza sola.
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX002')
        farmacia = Farmacia.objects.create(codigo='MC001', grupo=grupo, unidad_negocio=sg)
        estacion_nueva = Estacion.objects.create(codigo='MC001-C', farmacia=farmacia)

        procesados = self.adaptador._registrar_nodo('node/domain0/nuevoNodeId', 1, 'MC001-C')

        self.assertEqual(procesados, 1)
        estacion_nueva.refresh_from_db()
        self.assertEqual(estacion_nueva.meshcentral_node_id, 'nuevoNodeId')
        self.assertIsNotNone(estacion_nueva.meshcentral_vinculado_en)
        self.assertTrue(
            EstadoDispositivo.objects.filter(estacion=estacion_nueva, fuente=EstadoDispositivo.Fuente.MESHCENTRAL).exists(),
        )

    def test_registrar_nodo_no_pisa_un_vinculo_existente(self):
        # self.estacion ya tiene meshcentral_node_id='abc123' — un nombre que
        # coincidiera con su código no debería reemplazar ese vínculo.
        self.estacion.codigo = 'NOMBRE-DUPLICADO'
        self.estacion.save(update_fields=['codigo'])

        procesados = self.adaptador._registrar_nodo('node/domain0/otroNodeId', 1, 'NOMBRE-DUPLICADO')

        self.assertEqual(procesados, 0)
        self.estacion.refresh_from_db()
        self.assertEqual(self.estacion.meshcentral_node_id, 'abc123')

    def test_registrar_nodo_sin_nombre_no_intenta_vincular(self):
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX002')
        farmacia = Farmacia.objects.create(codigo='MC001', grupo=grupo, unidad_negocio=sg)
        Estacion.objects.create(codigo='MC001-C', farmacia=farmacia)

        procesados = self.adaptador._registrar_nodo('node/domain0/nuevoNodeId', 1, '')
        self.assertEqual(procesados, 0)

    def test_procesar_mensaje_nodeconnect_actualiza_estado(self):
        self.adaptador._procesar_mensaje({
            'action': 'event',
            'event': {'action': 'nodeconnect', 'nodeid': 'node/domain0/abc123', 'conn': 0},
        })
        estado = EstadoDispositivo.objects.get(estacion=self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL)
        self.assertFalse(estado.en_linea)

    def test_procesar_mensaje_ignora_eventos_no_relacionados(self):
        self.adaptador._procesar_mensaje({'action': 'event', 'event': {'action': 'changenode'}})
        self.adaptador._procesar_mensaje({'action': 'serverBackup'})
        self.assertFalse(EstadoDispositivo.objects.exists())

    def test_procesar_nodes_recorre_todos_los_meshid(self):
        procesados = self.adaptador._procesar_nodes({
            'action': 'nodes',
            'nodes': {'mesh/domain0/xyz': [{'_id': 'node/domain0/abc123', 'conn': 1}]},
        })
        self.assertEqual(procesados, 1)

    @override_settings(MESHCENTRAL_API_CONFIG={})
    def test_configurado_false_por_defecto(self):
        # Explícito con override_settings (no depender de que el .env local no tenga
        # estas variables — en este entorno de desarrollo, por ejemplo, sí las tiene).
        self.assertFalse(AdaptadorMeshCentral().configurado())

    @override_settings(
        MESHCENTRAL_API_CONFIG={'WS_URL': 'wss://mesh.example.com/control.ashx', 'USUARIO': 'u', 'PASSWORD': 'p'},
    )
    def test_configurado_true_con_las_3_variables(self):
        self.assertTrue(AdaptadorMeshCentral().configurado())

    @override_settings(MESHCENTRAL_API_CONFIG={})
    def test_sincronizar_todo_no_configurado_no_hace_nada(self):
        self.assertEqual(AdaptadorMeshCentral().sincronizar_todo(), 0)

    def test_solicitar_nodes_reintenta_si_no_llega_respuesta_a_tiempo(self):
        """Regresión: verificado contra el servidor real de producción (13-ago-2026) que
        un "nodes" mandado inmediatamente después del login se pierde de forma
        consistente — el servidor todavía está armando la sesión. Sin este reintento,
        sincronizar_todo() se cuelga siempre en el primer llamado tras loguear."""
        ws = MagicMock()
        respuesta_nodes = json.dumps({
            'action': 'nodes',
            'nodes': {'mesh/domain0/xyz': [{'_id': 'node/domain0/abc123', 'conn': 1}]},
        })
        ws.recv.side_effect = [websocket.WebSocketTimeoutException('timeout'), respuesta_nodes]

        procesados = self.adaptador._solicitar_nodes(ws)

        self.assertEqual(procesados, 1)
        self.assertEqual(ws.send.call_count, 2)  # el pedido original + el reintento

    def test_solicitar_nodes_agota_reintentos_y_relanza(self):
        ws = MagicMock()
        ws.recv.side_effect = websocket.WebSocketTimeoutException('timeout')

        with self.assertRaises(websocket.WebSocketTimeoutException):
            self.adaptador._solicitar_nodes(ws, reintentos=1)

    def test_solicitar_nodes_procesa_eventos_intermedios_sin_descartarlos(self):
        ws = MagicMock()
        evento_intermedio = json.dumps({
            'action': 'event',
            'event': {'action': 'nodeconnect', 'nodeid': 'node/domain0/abc123', 'conn': 0},
        })
        respuesta_nodes = json.dumps({'action': 'nodes', 'nodes': {}})
        ws.recv.side_effect = [
            json.dumps({'action': 'serverinfo', 'serverinfo': {}}), evento_intermedio, respuesta_nodes,
        ]

        self.adaptador._solicitar_nodes(ws)

        estado = EstadoDispositivo.objects.get(estacion=self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL)
        self.assertFalse(estado.en_linea)

    def test_escuchar_eventos_no_reconecta_por_inactividad(self):
        """Regresión: verificado contra el servidor real de producción (14-ago-2026) que
        un timeout de recv() por simple inactividad (nadie mandó nada, el caso normal)
        se trataba como conexión perdida y forzaba reconectar (re-auth + resync
        completo) cada 8-15s en loop constante — disfrazaba el diseño "push, sin
        polling" en un poll agresivo."""
        import threading

        from django.test import override_settings

        detener = threading.Event()
        ws = MagicMock()

        respuesta_nodes = json.dumps({'action': 'nodes', 'nodes': {}})
        evento = json.dumps({
            'action': 'event',
            'event': {'action': 'nodeconnect', 'nodeid': 'node/domain0/abc123', 'conn': 1},
        })

        def _recv_side_effect():
            _recv_side_effect.llamadas += 1
            if _recv_side_effect.llamadas <= 3:
                raise websocket.WebSocketTimeoutException('timeout')
            if _recv_side_effect.llamadas == 4:
                return evento
            detener.set()
            raise websocket.WebSocketTimeoutException('timeout')

        _recv_side_effect.llamadas = 0
        ws.recv.side_effect = _recv_side_effect

        with override_settings(
            MESHCENTRAL_API_CONFIG={'WS_URL': 'wss://x', 'USUARIO': 'u', 'PASSWORD': 'p'},
        ):
            adaptador = AdaptadorMeshCentral()
            adaptador._autenticar_y_conectar = MagicMock(return_value=ws)
            adaptador._solicitar_nodes = MagicMock(return_value=0)

            adaptador.escuchar_eventos(detener=detener)

        adaptador._autenticar_y_conectar.assert_called_once()  # nunca reconectó
        estado = EstadoDispositivo.objects.get(estacion=self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL)
        self.assertTrue(estado.en_linea)


@override_settings(MIKROTIK_SNMP_CONFIG={'PUERTO': 161})
class SincronizarAnchoBandaFarmaciasTests(TestCase):
    """Parte A del monitoreo proactivo de red (SNMP a Mikrotik) — el cliente SNMP se
    mockea siempre (_sondear_farmacia), nunca se sondea hardware real en tests."""

    def setUp(self):
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.con_ip = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg, ip_router='10.0.1.1')
        self.sin_ip = Farmacia.objects.create(codigo='ML002', grupo=grupo, unidad_negocio=sg)

    def test_sin_farmacias_con_ip_router_no_hace_nada(self):
        self.con_ip.delete()
        with patch('apps.monitoreo.mikrotik._sondear_farmacia', new_callable=AsyncMock) as sondear:
            n = sincronizar_ancho_banda_farmacias()
        self.assertEqual(n, 0)
        sondear.assert_not_called()

    def test_comunidad_se_deriva_del_codigo_de_farmacia_en_minuscula(self):
        # Convención confirmada contra un router real de producción (20-ago-2026):
        # la community SNMP de cada Mikrotik es el código de su farmacia en
        # minúscula, no una community global compartida.
        from apps.monitoreo.mikrotik import _comunidad_para
        self.assertEqual(_comunidad_para(self.con_ip), 'ml001')

    def test_farmacia_sin_ip_router_no_se_sondea(self):
        with patch('apps.monitoreo.mikrotik._sondear_farmacia', new_callable=AsyncMock) as sondear:
            sondear.return_value = (self.con_ip, 1000, 500)
            sincronizar_ancho_banda_farmacias()
        sondear.assert_called_once()
        self.assertEqual(sondear.call_args[0][0], self.con_ip)

    def test_router_caido_no_interrumpe_el_resto(self):
        otra = Farmacia.objects.create(
            codigo='ML003', grupo=self.con_ip.grupo, unidad_negocio=self.con_ip.unidad_negocio, ip_router='10.0.1.2',
        )

        async def side_effect(farmacia, *args):
            return None if farmacia.pk == self.con_ip.pk else (farmacia, 1000, 500)

        with patch('apps.monitoreo.mikrotik._sondear_farmacia', side_effect=side_effect):
            n = sincronizar_ancho_banda_farmacias()

        self.assertEqual(n, 1)
        self.assertFalse(MuestraRedFarmacia.objects.filter(farmacia=self.con_ip).exists())
        self.assertTrue(MuestraRedFarmacia.objects.filter(farmacia=otra).exists())

    def test_primera_muestra_no_calcula_tasa(self):
        with patch('apps.monitoreo.mikrotik._sondear_farmacia', new_callable=AsyncMock) as sondear:
            sondear.return_value = (self.con_ip, 100_000, 50_000)
            sincronizar_ancho_banda_farmacias()

        muestra = MuestraRedFarmacia.objects.get(farmacia=self.con_ip)
        self.assertEqual(muestra.bytes_recibidos, 100_000)
        self.assertIsNone(muestra.red_recibido_kbps)
        self.assertIsNone(muestra.red_enviado_kbps)

    def test_segunda_muestra_calcula_la_tasa_contra_la_anterior(self):
        anterior = MuestraRedFarmacia.objects.create(
            farmacia=self.con_ip, bytes_recibidos=100_000_000, bytes_enviados=50_000_000,
        )
        MuestraRedFarmacia.objects.filter(pk=anterior.pk).update(timestamp=timezone.now() - timedelta(seconds=300))

        with patch('apps.monitoreo.mikrotik._sondear_farmacia', new_callable=AsyncMock) as sondear:
            # +12.000.000 bytes recibidos en 300s -> 12e6*8/1000/300 = 320 kbps
            # +6.000.000 bytes enviados en 300s -> 6e6*8/1000/300 = 160 kbps
            sondear.return_value = (self.con_ip, 112_000_000, 56_000_000)
            sincronizar_ancho_banda_farmacias()

        muestra = MuestraRedFarmacia.objects.filter(farmacia=self.con_ip).exclude(pk=anterior.pk).get()
        self.assertEqual(muestra.red_recibido_kbps, 320.0)
        self.assertEqual(muestra.red_enviado_kbps, 160.0)

    def test_contador_reiniciado_no_calcula_tasa_negativa(self):
        # Router reiniciado entre corridas: el contador vuelve a empezar desde ~0,
        # menor que la muestra anterior — no debe calcular una tasa negativa/sin
        # sentido, se retoma normal en la próxima corrida.
        anterior = MuestraRedFarmacia.objects.create(
            farmacia=self.con_ip, bytes_recibidos=100_000_000, bytes_enviados=50_000_000,
        )
        MuestraRedFarmacia.objects.filter(pk=anterior.pk).update(timestamp=timezone.now() - timedelta(seconds=300))

        with patch('apps.monitoreo.mikrotik._sondear_farmacia', new_callable=AsyncMock) as sondear:
            sondear.return_value = (self.con_ip, 500, 200)
            sincronizar_ancho_banda_farmacias()

        muestra = MuestraRedFarmacia.objects.filter(farmacia=self.con_ip).exclude(pk=anterior.pk).get()
        self.assertIsNone(muestra.red_recibido_kbps)
        self.assertIsNone(muestra.red_enviado_kbps)


class SolicitarSondeoRedFarmaciasViaAgenteTests(TestCase):
    """El servidor no tiene ruta de red hacia las IPs privadas de las farmacias
    (confirmado 24-ago-2026) -- esto reemplaza en la práctica al sondeo directo,
    pidiéndole a una estación de la propia LAN de cada farmacia que sondee su
    Mikrotik local y reporte por MQTT."""

    def setUp(self):
        from .mikrotik import solicitar_sondeo_red_farmacias_via_agente
        self.solicitar = solicitar_sondeo_red_farmacias_via_agente

        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg, ip_router='10.0.1.1')

    def test_le_pide_a_una_estacion_online_de_la_farmacia(self):
        estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=self.farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA, estado_conexion=Estacion.EstadoConexion.ONLINE,
        )
        with patch('apps.catalogo.services.enviar_consultar_red_farmacia', return_value=True) as mock_enviar:
            n = self.solicitar()
        self.assertEqual(n, 1)
        mock_enviar.assert_called_once_with(estacion, 'ml001')

    def test_farmacia_sin_ip_router_se_ignora(self):
        self.farmacia.ip_router = ''
        self.farmacia.save(update_fields=['ip_router'])
        Estacion.objects.create(
            codigo='ML001-A', farmacia=self.farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA, estado_conexion=Estacion.EstadoConexion.ONLINE,
        )
        with patch('apps.catalogo.services.enviar_consultar_red_farmacia', return_value=True) as mock_enviar:
            n = self.solicitar()
        self.assertEqual(n, 0)
        mock_enviar.assert_not_called()

    def test_farmacia_sin_estacion_online_se_ignora(self):
        Estacion.objects.create(
            codigo='ML001-A', farmacia=self.farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA, estado_conexion=Estacion.EstadoConexion.OFFLINE,
        )
        with patch('apps.catalogo.services.enviar_consultar_red_farmacia', return_value=True) as mock_enviar:
            n = self.solicitar()
        self.assertEqual(n, 0)
        mock_enviar.assert_not_called()

    def test_una_sola_estacion_por_farmacia_aunque_haya_varias_online(self):
        Estacion.objects.create(
            codigo='ML001-A', farmacia=self.farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA, estado_conexion=Estacion.EstadoConexion.ONLINE,
        )
        Estacion.objects.create(
            codigo='ML001-B', farmacia=self.farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA, estado_conexion=Estacion.EstadoConexion.ONLINE,
        )
        with patch('apps.catalogo.services.enviar_consultar_red_farmacia', return_value=True) as mock_enviar:
            n = self.solicitar()
        self.assertEqual(n, 1)
        mock_enviar.assert_called_once()


class AlertaAbreMantenimientoTests(TestCase):
    """Cierra el círculo del RMM: una alerta de una regla marcada abre sola la orden
    de trabajo. Nunca debe romper el flujo de alertas si algo falta."""

    def setUp(self):
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg)
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self.usuario = User.objects.create_user(username='u_rmm', password='x')
        self.regla = ReglaAlerta.objects.create(
            nombre='POS caído', metrica=Metrica.SIN_HEARTBEAT, umbral=90, duracion_minutos=5,
            severidad=ReglaAlerta.Severidad.CRITICAL, unidad_negocio=sg, creado_por=self.usuario,
            abre_mantenimiento=True,
        )
        self.activo = Activo.objects.create(
            codigo='CR-DSK-7001', tipo=Activo.Tipo.DESKTOP, estacion=self.estacion,
        )

    def test_abre_el_mantenimiento_y_lo_deja_enlazado(self):
        alerta = abrir_o_mantener_alerta(self.regla, self.estacion, valor=95)
        alerta.refresh_from_db()
        self.assertIsNotNone(alerta.mantenimiento)
        m = alerta.mantenimiento
        self.assertEqual(m.tipo_origen, TipoOrigenMantenimiento.MONITOREO)
        # Regla crítica -> prioridad crítica (SLA de 4h, no el de un preventivo).
        self.assertEqual(m.prioridad, PrioridadMantenimiento.CRITICA)
        self.assertEqual(list(m.equipos.values_list('equipo__codigo', flat=True)), ['CR-DSK-7001'])

    def test_regla_de_advertencia_entra_como_alta_no_critica(self):
        self.regla.severidad = ReglaAlerta.Severidad.WARNING
        self.regla.save(update_fields=['severidad'])
        alerta = abrir_o_mantener_alerta(self.regla, self.estacion, valor=95)
        alerta.refresh_from_db()
        self.assertEqual(alerta.mantenimiento.prioridad, PrioridadMantenimiento.ALTA)

    def test_regla_sin_el_flag_no_abre_nada(self):
        self.regla.abre_mantenimiento = False
        self.regla.save(update_fields=['abre_mantenimiento'])
        alerta = abrir_o_mantener_alerta(self.regla, self.estacion, valor=95)
        alerta.refresh_from_db()
        self.assertIsNone(alerta.mantenimiento)
        self.assertFalse(Mantenimiento.objects.exists())

    def test_estacion_sin_activo_vinculado_no_rompe_la_alerta(self):
        self.activo.estacion = None
        self.activo.save(update_fields=['estacion'])
        alerta = abrir_o_mantener_alerta(self.regla, self.estacion, valor=95)
        # La alerta se abre igual: no poder crear la orden no puede tragarse el aviso.
        self.assertIsNotNone(alerta)
        alerta.refresh_from_db()
        self.assertIsNone(alerta.mantenimiento)

    def test_no_duplica_si_el_equipo_ya_tiene_un_mantenimiento_abierto(self):
        alerta1 = abrir_o_mantener_alerta(self.regla, self.estacion, valor=95)
        self.assertIsNotNone(alerta1.mantenimiento)
        # Se resuelve y vuelve a dispararse: no debe abrir una segunda orden sobre el
        # mismo equipo mientras la primera siga abierta.
        resolver_condicion(self.regla, self.estacion)
        alerta2 = abrir_o_mantener_alerta(self.regla, self.estacion, valor=97)
        alerta2.refresh_from_db()
        self.assertIsNone(alerta2.mantenimiento)
        self.assertEqual(Mantenimiento.objects.count(), 1)


class SondeoEnlacesTests(TestCase):
    """Sondeo ICMP del enlace de cada farmacia (apps/monitoreo/enlaces.py)."""

    def setUp(self):
        from apps.catalogo.models import Farmacia, Grupo, UnidadNegocio

        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia = Farmacia.objects.create(
            codigo='ML001', grupo=grupo, unidad_negocio=sg,
            ip_router='192.168.102.1', circuito_proveedor='sangregorio2-santana',
        )

    def _fallar(self, veces):
        from apps.monitoreo.enlaces import registrar_sondeo
        for _ in range(veces):
            registrar_sondeo(self.farmacia, False, None)
        return self.farmacia.estado_enlace

    def test_una_falla_suelta_no_declara_caida(self):
        """Un paquete ICMP se pierde por mil motivos. Declarar la caída al primer fallo
        llenaría el panel de caídas que no ocurrieron."""
        from apps.monitoreo.models import EstadoEnlaceFarmacia, EventoEnlaceFarmacia

        estado = self._fallar(1)
        self.assertEqual(estado.fallas_consecutivas, 1)
        # Sigue en null: nunca se confirmó que estuviera viva, y tampoco que esté caída.
        self.assertIsNone(estado.alcanzable)
        self.assertFalse(EventoEnlaceFarmacia.objects.exists())

        estado = self._fallar(EstadoEnlaceFarmacia.UMBRAL_FALLAS_CONSECUTIVAS - 1)
        self.assertFalse(estado.alcanzable)
        self.assertEqual(EventoEnlaceFarmacia.objects.count(), 1)

    def test_la_recuperacion_cierra_el_evento_con_su_duracion(self):
        from apps.monitoreo.enlaces import registrar_sondeo
        from apps.monitoreo.models import EstadoEnlaceFarmacia, EventoEnlaceFarmacia

        self._fallar(EstadoEnlaceFarmacia.UMBRAL_FALLAS_CONSECUTIVAS)
        evento = EventoEnlaceFarmacia.objects.get()
        self.assertTrue(evento.en_curso)
        self.assertEqual(evento.circuito_proveedor, 'sangregorio2-santana')

        registrar_sondeo(self.farmacia, True, 32.5)
        evento.refresh_from_db()
        self.assertFalse(evento.en_curso)
        self.assertIsNotNone(evento.duracion_minutos)

        estado = self.farmacia.estado_enlace
        estado.refresh_from_db()
        self.assertTrue(estado.alcanzable)
        self.assertEqual(estado.fallas_consecutivas, 0)
        self.assertEqual(estado.latencia_ms, 32.5)

    def test_no_abre_un_segundo_evento_mientras_sigue_caida(self):
        from apps.monitoreo.models import EstadoEnlaceFarmacia, EventoEnlaceFarmacia

        self._fallar(EstadoEnlaceFarmacia.UMBRAL_FALLAS_CONSECUTIVAS + 5)
        self.assertEqual(EventoEnlaceFarmacia.objects.count(), 1)

    def test_el_barrido_se_aborta_si_casi_todo_falla(self):
        """La guarda que evita el error que ya cometió mikrotik.py: correr esto desde un
        host sin ruta reportaría toda la flota caída. 704 farmacias no se caen a la vez."""
        from apps.catalogo.models import Farmacia, Grupo
        from apps.monitoreo.enlaces import sondear_enlaces_farmacias
        from apps.monitoreo.models import EstadoEnlaceFarmacia, EventoEnlaceFarmacia

        grupo = Grupo.objects.get(codigo='TRX001')
        for i in range(2, 6):
            Farmacia.objects.create(
                codigo=f'ML00{i}', grupo=grupo, unidad_negocio=self.farmacia.unidad_negocio,
                ip_router=f'192.168.102.{i}',
            )

        with patch('apps.monitoreo.enlaces.sondear_enlace', return_value=(False, None)):
            resumen = sondear_enlaces_farmacias()

        self.assertTrue(resumen['abortado'])
        self.assertEqual(resumen['sondeadas'], 5)
        # Lo que importa: no escribió NADA.
        self.assertFalse(EstadoEnlaceFarmacia.objects.exists())
        self.assertFalse(EventoEnlaceFarmacia.objects.exists())

    def test_el_barrido_registra_cuando_la_ruta_funciona(self):
        from apps.catalogo.models import Farmacia, Grupo
        from apps.monitoreo.enlaces import sondear_enlaces_farmacias
        from apps.monitoreo.models import EstadoEnlaceFarmacia

        grupo = Grupo.objects.get(codigo='TRX001')
        for i in range(2, 6):
            Farmacia.objects.create(
                codigo=f'ML00{i}', grupo=grupo, unidad_negocio=self.farmacia.unidad_negocio,
                ip_router=f'192.168.102.{i}',
            )

        with patch('apps.monitoreo.enlaces.sondear_enlace', return_value=(True, 21.0)):
            resumen = sondear_enlaces_farmacias()

        self.assertFalse(resumen['abortado'])
        self.assertEqual(resumen['activas'], 5)
        self.assertEqual(EstadoEnlaceFarmacia.objects.filter(alcanzable=True).count(), 5)

    def test_ignora_farmacias_sin_ip_cargada(self):
        """GenericIPAddressField normaliza '' a None; el filtro tiene que ser __isnull."""
        from apps.catalogo.models import Farmacia, Grupo
        from apps.monitoreo.enlaces import sondear_enlaces_farmacias

        grupo = Grupo.objects.get(codigo='TRX001')
        Farmacia.objects.create(
            codigo='ML099', grupo=grupo, unidad_negocio=self.farmacia.unidad_negocio, ip_router=None,
        )
        with patch('apps.monitoreo.enlaces.sondear_enlace', return_value=(True, 10.0)):
            resumen = sondear_enlaces_farmacias()
        self.assertEqual(resumen['sondeadas'], 1)

    def test_parsea_la_latencia_en_ingles_y_en_espanol(self):
        """La salida de `ping` cambia con el idioma del SO; el servidor corre en un
        contenedor Linux en inglés y las máquinas de oficina son Windows en español."""
        from apps.monitoreo.enlaces import sondear_enlace

        for salida, esperado in (
            ('Reply from 192.168.102.1: bytes=32 time=24ms TTL=61', 24.0),
            ('Respuesta desde 192.168.102.1: bytes=32 tiempo=13ms TTL=61', 13.0),
            ('64 bytes from 192.168.102.1: icmp_seq=1 ttl=61 time=8.42 ms', 8.42),
            # Windows en español, respuesta submilisegundo: imprime "tiempo<1m", SIN la s
            # de ms. Exigir "ms" en el regex hacía que estos sondeos quedaran vivos pero
            # sin latencia — encontrado corriendo el comando de verdad.
            ('Respuesta desde 127.0.0.1: bytes=32 tiempo<1m TTL=128', 1.0),
        ):
            with patch('apps.monitoreo.enlaces.subprocess.run') as mock_run:
                mock_run.return_value.returncode = 0
                mock_run.return_value.stdout = salida
                self.assertEqual(sondear_enlace('192.168.102.1'), (True, esperado))

    def test_host_inaccesible_no_cuenta_como_vivo(self):
        """`ping` en Windows devuelve 0 aunque responda "Host de destino inaccesible",
        que es un ICMP de OTRO equipo de la ruta, no del destino."""
        from apps.monitoreo.enlaces import sondear_enlace

        with patch('apps.monitoreo.enlaces.subprocess.run') as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = 'Respuesta desde 10.111.6.1: Host de destino inaccesible.'
            self.assertEqual(sondear_enlace('192.168.102.1'), (False, None))


class SondeoEnlaceAPITests(TestCase):
    """Ingesta de sondeos por API (apps/monitoreo/api_views.py).

    Existe para el caso en que el host con ruta a las farmacias no pueda alcanzar la base
    de datos: la sonda mide y reporta por HTTP, reusando el mismo registrar_sondeo().
    """

    def setUp(self):
        from rest_framework.authtoken.models import Token

        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.ml001 = Farmacia.objects.create(
            codigo='ML001', grupo=grupo, unidad_negocio=self.sg, ip_router='192.168.102.1',
        )
        self.ml002 = Farmacia.objects.create(
            codigo='ML002', grupo=grupo, unidad_negocio=self.sg, ip_router='192.168.102.2',
        )
        self.ajena = Farmacia.objects.create(
            codigo='MAM01', grupo=grupo, unidad_negocio=self.mia, ip_router='10.101.18.225',
        )

        self.sonda = User.objects.create_user(username='sonda-sg', password='x')
        PerfilUsuario.objects.create(usuario=self.sonda, acceso_todas_unidades=False).unidades_negocio.set([self.sg])
        self.sonda.user_permissions.add(
            Permission.objects.get(content_type__app_label='monitoreo', codename='registrar_sondeo_enlace'),
        )
        self.token = Token.objects.create(user=self.sonda)

        self.url = reverse('api-enlaces-sondeo')

    def _post(self, resultados, token=None):
        return self.client.post(
            self.url, {'resultados': resultados}, content_type='application/json',
            HTTP_AUTHORIZATION=f'Token {token or self.token.key}',
        )

    def test_registra_el_barrido(self):
        from apps.monitoreo.models import EstadoEnlaceFarmacia

        resp = self._post([
            {'farmacia': 'ML001', 'alcanzable': True, 'latencia_ms': 21.5},
            {'farmacia': 'ML002', 'alcanzable': False},
        ])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['registrados'], 2)
        self.assertEqual(resp.json()['activas'], 1)

        estado = EstadoEnlaceFarmacia.objects.get(farmacia=self.ml001)
        self.assertTrue(estado.alcanzable)
        self.assertEqual(estado.latencia_ms, 21.5)

    def test_exige_el_permiso_propio_no_solo_estar_autenticado(self):
        """El token de la sonda vive en una máquina de oficina, fuera del servidor: si se
        filtra tiene que servir para reportar mediciones y para nada más."""
        from rest_framework.authtoken.models import Token

        pelado = User.objects.create_user(username='sin_permiso_api', password='x')
        PerfilUsuario.objects.create(usuario=pelado, acceso_todas_unidades=True)
        token = Token.objects.create(user=pelado)

        resp = self._post([{'farmacia': 'ML001', 'alcanzable': True}], token=token.key)
        self.assertEqual(resp.status_code, 403)

    def test_sin_token_no_entra(self):
        resp = self.client.post(
            self.url, {'resultados': [{'farmacia': 'ML001', 'alcanzable': True}]},
            content_type='application/json',
        )
        self.assertIn(resp.status_code, (401, 403))

    def test_rechaza_el_barrido_si_casi_todo_fallo(self):
        """Una sonda que perdió su ruta reportaría toda la flota caída. Misma guarda que
        el barrido local, y por el mismo motivo."""
        from apps.monitoreo.models import EstadoEnlaceFarmacia

        resp = self._post([
            {'farmacia': 'ML001', 'alcanzable': False},
            {'farmacia': 'ML002', 'alcanzable': False},
        ])
        self.assertEqual(resp.status_code, 409)
        self.assertTrue(resp.json()['abortado'])
        self.assertFalse(EstadoEnlaceFarmacia.objects.exists())

    def test_no_puede_reportar_farmacias_de_otra_unidad_de_negocio(self):
        from apps.monitoreo.models import EstadoEnlaceFarmacia

        resp = self._post([
            {'farmacia': 'ML001', 'alcanzable': True},
            {'farmacia': 'MAM01', 'alcanzable': True},
        ])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['registrados'], 1)
        self.assertEqual(resp.json()['desconocidas'], ['MAM01'])
        self.assertFalse(EstadoEnlaceFarmacia.objects.filter(farmacia=self.ajena).exists())

    def test_rechaza_farmacias_repetidas_en_el_mismo_barrido(self):
        """Cuál de las dos gana sería arbitrario, y una contaría mal en la guarda."""
        resp = self._post([
            {'farmacia': 'ML001', 'alcanzable': True},
            {'farmacia': 'ML001', 'alcanzable': False},
        ])
        self.assertEqual(resp.status_code, 400)
        self.assertIn('repetidas', str(resp.json()))

    def test_rechaza_un_lote_vacio(self):
        self.assertEqual(self._post([]).status_code, 400)

    def test_entrega_la_lista_de_farmacias_a_sondear(self):
        """La sonda no mantiene su propia copia de las IP: esa copia es lo que se
        desincroniza cuando abre una farmacia o cambia una IP."""
        resp = self.client.get(
            reverse('api-enlaces-farmacias'), HTTP_AUTHORIZATION=f'Token {self.token.key}',
        )
        self.assertEqual(resp.status_code, 200)
        codigos = [f['codigo'] for f in resp.json()['farmacias']]
        # Solo las de su alcance: MAM01 es de MIA.
        self.assertEqual(codigos, ['ML001', 'ML002'])
        self.assertEqual(resp.json()['farmacias'][0]['ip'], '192.168.102.1')

    def test_la_lista_tambien_exige_el_permiso(self):
        from rest_framework.authtoken.models import Token

        pelado = User.objects.create_user(username='sin_permiso_lista', password='x')
        PerfilUsuario.objects.create(usuario=pelado, acceso_todas_unidades=True)
        token = Token.objects.create(user=pelado)
        resp = self.client.get(
            reverse('api-enlaces-farmacias'), HTTP_AUTHORIZATION=f'Token {token.key}',
        )
        self.assertEqual(resp.status_code, 403)


class UnidadesDeAnchoDeBandaTests(TestCase):
    """La tasa de red se calcula en kilobits y tiene que etiquetarse como kilobits.

    Hasta el 15-sep-2026 las pantallas de enlaces decían "KB/s" —kilobytes— sobre un
    número que es kbps: ocho veces más. Con MC001 en 831 kbps, alguien que comparara
    contra un contrato de 10 Mbps leía 6,6 Mbps (66% del enlace) cuando el consumo real
    era 0,83 Mbps (8%).

    Peor: `Metrica.RED_TOTAL_KBPS` llevaba esa etiqueta en el desplegable de crear reglas
    de alerta, así que un umbral "Red > 500" puesto pensando en kilobytes habría
    disparado a 62,5 KB/s reales. No llegó a pasar —había 0 reglas sobre esa métrica—
    pero le tocaba al primero que creara una.
    """

    def test_la_tasa_se_calcula_en_kilobits(self):
        """1.000.000 de bytes en 10 s son 800 kbps (×8 bits, ÷1000). Si alguien cambiara
        la fórmula a kilobytes daría 100 y esta prueba lo atrapa."""
        from datetime import timedelta

        from django.utils import timezone

        from apps.catalogo.models import Farmacia, Grupo, UnidadNegocio
        from apps.monitoreo.mikrotik import _calcular_tasa
        from apps.monitoreo.models import MuestraRedFarmacia

        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg)

        anterior = MuestraRedFarmacia.objects.create(
            farmacia=farmacia, bytes_recibidos=0, bytes_enviados=0,
        )
        MuestraRedFarmacia.objects.filter(pk=anterior.pk).update(
            timestamp=timezone.now() - timedelta(seconds=10),
        )

        recibido, enviado = _calcular_tasa(farmacia, 1_000_000, 500_000)
        self.assertAlmostEqual(recibido, 800, delta=10)
        self.assertAlmostEqual(enviado, 400, delta=10)

    def test_la_etiqueta_de_la_metrica_dice_kbps(self):
        """Es la que ve quien crea una regla de alerta y elige un umbral."""
        from apps.monitoreo.models import Metrica

        etiqueta = dict(Metrica.choices)[Metrica.RED_TOTAL_KBPS]
        self.assertIn('kbps', etiqueta)
        self.assertNotIn('KB/s', etiqueta)

    def test_ninguna_plantilla_etiqueta_kilobits_como_kilobytes(self):
        """Regresión invisible para el resto de la suite: la página sigue devolviendo 200
        y el número sigue siendo correcto — solo la unidad miente, y por un factor de 8.
        """
        from pathlib import Path

        from django.conf import settings

        culpables = []
        for ruta in sorted((Path(settings.BASE_DIR) / 'templates').rglob('*.html')):
            contenido = ruta.read_text(encoding='utf-8')
            if 'KB/s' in contenido or 'kb/s' in contenido:
                culpables.append(ruta.name)
        self.assertEqual(
            culpables, [],
            'estas plantillas etiquetan kilobits como kilobytes: %s. La tasa se calcula '
            'en kbps (bytes * 8 / 1000), así que "KB/s" es ocho veces el valor real.'
            % ', '.join(culpables),
        )


class IdentidadEquipoBordeTests(TestCase):
    """Lectura SNMP de la identidad del Mikrotik (`sincronizar_identidad_equipos`).

    Los OID se verificaron contra tres equipos reales el 15-sep-2026 (GNB01, MC001,
    MCAR3): los cinco que se piden contestan en los tres. Temperatura, voltaje y memoria
    no responden en ninguno —son RB941/RB951, sin esos sensores— y por eso no se piden.
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia = Farmacia.objects.create(
            codigo='GNB01', grupo=grupo, unidad_negocio=self.sg, ip_router='10.101.50.65',
        )

    def _respuesta(self, **cambios):
        """La respuesta real de GNB01, para no inventar formas de datos."""
        datos = {
            'modelo': 'RouterOS RB951Ui-2nD',
            'uptime_segundos': 1170673,
            'nombre_sistema': 'GNB01',
            'version_routeros': '6.49.17',
            'numero_serie': 'HH70A5GKB55',
        }
        datos.update(cambios)
        return datos

    def _sincronizar(self, respuesta):
        from apps.monitoreo.mikrotik import sincronizar_identidad_equipos

        async def _falso(ip, comunidad, puerto):
            return respuesta

        with patch('apps.monitoreo.mikrotik._leer_identidad', _falso):
            return sincronizar_identidad_equipos([self.farmacia])

    def test_guarda_lo_que_reporta_el_equipo(self):
        from apps.monitoreo.models import EquipoBordeFarmacia

        resumen = self._sincronizar(self._respuesta())

        equipo = EquipoBordeFarmacia.objects.get(farmacia=self.farmacia)
        self.assertEqual(equipo.numero_serie, 'HH70A5GKB55')
        self.assertEqual(equipo.version_routeros, '6.49.17')
        self.assertEqual(equipo.modelo, 'RouterOS RB951Ui-2nD')
        self.assertIsNotNone(equipo.ultima_lectura)
        self.assertEqual(resumen['leidos'], 1)
        self.assertEqual(resumen['versiones'], {'6.49.17': 1})

    def test_una_segunda_lectura_actualiza_y_no_duplica(self):
        """El equipo es uno solo por farmacia: `update_or_create`, no una fila por
        corrida. El historial de tráfico sí es serie; esto no."""
        from apps.monitoreo.models import EquipoBordeFarmacia

        self._sincronizar(self._respuesta())
        self._sincronizar(self._respuesta(version_routeros='7.1.5'))

        self.assertEqual(EquipoBordeFarmacia.objects.count(), 1)
        self.assertEqual(EquipoBordeFarmacia.objects.get().version_routeros, '7.1.5')

    def test_detecta_que_la_ip_apunta_a_otro_equipo(self):
        """Verificación de integridad sobre las ~700 IP cargadas desde una planilla: si
        el equipo dice llamarse distinto, todo lo que se monitorea de esta farmacia es
        en realidad de otra."""
        from apps.monitoreo.models import EquipoBordeFarmacia

        resumen = self._sincronizar(self._respuesta(nombre_sistema='MCAR3'))

        equipo = EquipoBordeFarmacia.objects.get()
        self.assertFalse(equipo.nombre_coincide)
        self.assertEqual(len(resumen['nombres_discrepantes']), 1)
        self.assertIn('MCAR3', resumen['nombres_discrepantes'][0])

    def test_el_nombre_que_coincide_no_se_reporta(self):
        resumen = self._sincronizar(self._respuesta())
        self.assertEqual(resumen['nombres_discrepantes'], [])

    def test_sin_nombre_no_se_acusa_de_discrepancia(self):
        """Un equipo que no reporta sysName no es un equipo mal asignado."""
        from apps.monitoreo.models import EquipoBordeFarmacia

        self._sincronizar(self._respuesta(nombre_sistema=''))
        self.assertIsNone(EquipoBordeFarmacia.objects.get().nombre_coincide)

    def test_un_equipo_que_no_responde_no_rompe_la_corrida(self):
        """A 15-sep-2026 son 696 de 700 los que no tienen SNMP: que no respondan es lo
        normal, no un error."""
        from apps.monitoreo.models import EquipoBordeFarmacia

        resumen = self._sincronizar(None)

        self.assertEqual(resumen['leidos'], 0)
        self.assertEqual(resumen['sin_responder'], 1)
        self.assertEqual(EquipoBordeFarmacia.objects.count(), 0)

    def test_marca_un_equipo_recien_reiniciado(self):
        """Un router que se reinicia solo responde perfecto al ping entre reinicio y
        reinicio, así que el monitoreo de enlace no lo distingue de uno estable."""
        from apps.monitoreo.models import EquipoBordeFarmacia

        self._sincronizar(self._respuesta(uptime_segundos=600))
        self.assertTrue(EquipoBordeFarmacia.objects.get().reinicio_reciente)

        self._sincronizar(self._respuesta(uptime_segundos=1170673))
        self.assertFalse(EquipoBordeFarmacia.objects.get().reinicio_reciente)

    def test_el_texto_snmp_descarta_los_marcadores_de_oid_inexistente(self):
        """pysnmp devuelve "No Such Object..." en vez de un error cuando el equipo no
        conoce el OID. Sin filtrarlo, esa frase quedaría guardada como número de serie."""
        from apps.monitoreo.mikrotik import _texto_snmp

        self.assertEqual(_texto_snmp('No Such Object currently exists at this OID'), '')
        self.assertEqual(_texto_snmp('No more variables left in this MIB View'), '')
        self.assertEqual(_texto_snmp('  HH70A5GKB55  '), 'HH70A5GKB55')

    def test_el_comando_informa_la_brecha_de_versiones(self):
        from apps.monitoreo.models import EquipoBordeFarmacia

        otra = Farmacia.objects.create(
            codigo='MC001', grupo=self.farmacia.grupo, unidad_negocio=self.sg,
            ip_router='10.101.24.225',
        )
        EquipoBordeFarmacia.objects.create(farmacia=self.farmacia, version_routeros='6.49.17')
        EquipoBordeFarmacia.objects.create(farmacia=otra, version_routeros='6.47.7')

        salida = io.StringIO()
        with patch('apps.monitoreo.mikrotik._leer_identidad') as falso:
            async def _sin_respuesta(*a, **k):
                return None
            falso.side_effect = _sin_respuesta
            call_command('sondear_identidad_mikrotik', '--farmacias', 'GNB01,MC001', stdout=salida)
        self.assertIn('Sin responder: 2', salida.getvalue())

    def test_el_comando_falla_si_la_farmacia_no_tiene_ip(self):
        from django.core.management.base import CommandError

        Farmacia.objects.create(codigo='ML999', grupo=self.farmacia.grupo, unidad_negocio=self.sg)
        with self.assertRaises(CommandError):
            call_command('sondear_identidad_mikrotik', '--farmacias', 'ML999', stdout=io.StringIO())


class DescubrimientoPorArpTests(TestCase):
    """Descubrimiento de equipos por la tabla ARP del Mikrotik.

    Los datos de las pruebas son los que devolvió MCAR3 de verdad el 15-sep-2026: ocho
    entradas, una del gateway del proveedor por `ether3_TELCO` (ifIndex 3) y siete de la
    LAN por `farmamia` (ifIndex 8).
    """

    # Tal como llega de pysnmp: MAC en hexadecimal con 0x, y el OID con ifIndex + IP.
    ARP_MCAR3 = [
        ('1.3.6.1.2.1.4.22.1.2.3.10.107.128.17', '0xecf40c63cfe0'),
        ('1.3.6.1.2.1.4.22.1.2.8.10.101.41.194', '0x48210b5c7247'),
        ('1.3.6.1.2.1.4.22.1.2.8.10.101.41.195', '0xd0ad08586165'),
        ('1.3.6.1.2.1.4.22.1.2.8.10.101.41.205', '0x18b6f7762274'),
    ]

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia = Farmacia.objects.create(
            codigo='MCAR3', grupo=grupo, unidad_negocio=self.sg, ip_router='10.101.41.193',
        )

    def _sincronizar(self, filas):
        """`filas` son (mac, ip, ifIndex) ya parseadas, como devuelve `_leer_tabla_arp`."""
        from apps.monitoreo.mikrotik import sincronizar_dispositivos_detectados

        async def _falso(ip, comunidad, puerto):
            return filas

        with patch('apps.monitoreo.mikrotik._leer_tabla_arp', _falso):
            return sincronizar_dispositivos_detectados([self.farmacia])

    def _parseadas(self):
        from apps.monitoreo.mikrotik import _ip_e_indice_desde_oid, _normalizar_mac

        return [
            (_normalizar_mac(mac), _ip_e_indice_desde_oid(oid)[1], _ip_e_indice_desde_oid(oid)[0])
            for oid, mac in self.ARP_MCAR3
        ]

    # --- parseo de lo que entrega SNMP ---

    def test_normaliza_la_mac_al_formato_de_activo(self):
        """Se guarda igual que `Activo.mac` para que el cruce sea comparar texto."""
        from apps.monitoreo.mikrotik import _normalizar_mac

        self.assertEqual(_normalizar_mac('0xd0ad08586165'), 'D0:AD:08:58:61:65')
        self.assertEqual(_normalizar_mac('d0-ad-08-58-61-65'), 'D0:AD:08:58:61:65')
        self.assertEqual(_normalizar_mac('D0:AD:08:58:61:65'), 'D0:AD:08:58:61:65')

    def test_normaliza_el_objeto_que_llega_del_cable_y_no_solo_el_texto(self):
        """El bug que las pruebas anteriores no atraparon: le pasaban el texto ya
        formateado, pero de SNMP llega un OctetString cuyo `str()` da los bytes
        decodificados —basura binaria— y solo `prettyPrint()` da la forma `0x…`.

        Con `str()`, TODAS las MAC daban '' y el descubrimiento registraba cero equipos
        sin ningún error: la primera corrida en producción leyó las 4 farmacias y guardó
        nada.
        """
        from apps.monitoreo.mikrotik import _normalizar_mac

        class OctetStringFalso:
            """Imita a pysnmp: str() devuelve los bytes, prettyPrint() el hexadecimal."""

            def __init__(self, crudo):
                self._crudo = crudo

            def __str__(self):
                return self._crudo.decode('latin-1')

            def prettyPrint(self):
                return '0x' + self._crudo.hex()

        valor = OctetStringFalso(bytes.fromhex('d0ad08586165'))
        self.assertNotEqual(str(valor), '0xd0ad08586165')  # el modo que fallaba
        self.assertEqual(_normalizar_mac(valor), 'D0:AD:08:58:61:65')

    def test_descarta_una_mac_que_no_lo_es(self):
        """Mejor vacía que inventada: una fila con basura en la MAC rompería el cruce sin
        que nadie entienda por qué."""
        from apps.monitoreo.mikrotik import _normalizar_mac

        for basura in ('', '0x', 'no-es-una-mac', '0xd0ad0858616', '0xzzad08586165'):
            self.assertEqual(_normalizar_mac(basura), '')

    def test_saca_la_ip_y_la_interfaz_del_indice_del_oid(self):
        from apps.monitoreo.mikrotik import _ip_e_indice_desde_oid

        self.assertEqual(_ip_e_indice_desde_oid('1.3.6.1.2.1.4.22.1.2.8.10.101.41.194'), (8, '10.101.41.194'))
        self.assertEqual(_ip_e_indice_desde_oid('1.3.6.1.2.1.4.22.1.2.3.10.107.128.17'), (3, '10.107.128.17'))
        self.assertEqual(_ip_e_indice_desde_oid('1.2.3'), (None, None))

    # --- sincronización ---

    def test_registra_lo_que_ve_el_router(self):
        from apps.monitoreo.models import DispositivoDetectado

        resumen = self._sincronizar(self._parseadas())

        self.assertEqual(DispositivoDetectado.objects.count(), 4)
        self.assertEqual(resumen['nuevos'], 4)
        gateway = DispositivoDetectado.objects.get(ip='10.107.128.17')
        self.assertEqual(gateway.mac, 'EC:F4:0C:63:CF:E0')
        self.assertEqual(gateway.interfaz_indice, 3)

    def test_una_ip_nueva_para_la_misma_mac_actualiza_y_no_duplica(self):
        """La identidad es la MAC: la Epson va por WiFi con DHCP y cambia de dirección,
        pero sigue siendo el mismo equipo."""
        from apps.monitoreo.models import DispositivoDetectado

        self._sincronizar([('D0:AD:08:58:61:65', '10.101.41.195', 8)])
        self._sincronizar([('D0:AD:08:58:61:65', '10.101.41.250', 8)])

        self.assertEqual(DispositivoDetectado.objects.count(), 1)
        self.assertEqual(str(DispositivoDetectado.objects.get().ip), '10.101.41.250')

    def test_un_equipo_que_se_desconecta_no_se_borra(self):
        """Deja de actualizarse, que es lo que permite notar después que algo desapareció.
        Borrarlo haría imposible distinguir "nunca estuvo" de "ya no está"."""
        from apps.monitoreo.models import DispositivoDetectado

        self._sincronizar(self._parseadas())
        self._sincronizar([('D0:AD:08:58:61:65', '10.101.41.195', 8)])

        self.assertEqual(DispositivoDetectado.objects.count(), 4)

    def test_una_farmacia_que_no_responde_no_rompe_la_corrida(self):
        resumen = self._sincronizar(None)
        self.assertEqual(resumen['sin_responder'], 1)
        self.assertEqual(resumen['farmacias_leidas'], 0)

    # --- el cruce con lo declarado ---

    def test_lista_lo_conectado_que_nadie_inventario(self):
        resumen = self._sincronizar(self._parseadas())
        self.assertEqual(len(resumen['sin_declarar']), 4)

    def test_un_activo_con_esa_mac_deja_de_figurar_como_sin_declarar(self):
        """Es el cruce que da sentido a todo esto: lo descubierto contra lo declarado."""
        from apps.activos.models import Activo
        from apps.activos.services import generar_codigo_activo

        Activo.objects.create(
            codigo=generar_codigo_activo(Activo.Tipo.IMPRESORA), tipo=Activo.Tipo.IMPRESORA,
            farmacia=self.farmacia, unidad_negocio=self.sg, mac='D0:AD:08:58:61:65',
            estado=Activo.Estado.ASIGNADO,
        )
        resumen = self._sincronizar(self._parseadas())

        sin_declarar = ' '.join(resumen['sin_declarar'])
        self.assertNotIn('D0:AD:08:58:61:65', sin_declarar)
        self.assertEqual(len(resumen['sin_declarar']), 3)

    def test_el_cruce_no_distingue_mayusculas(self):
        """`Activo.mac` la carga una persona y puede venir en minúscula; la del router
        siempre llega normalizada. Una diferencia de grafía no puede hacer que un equipo
        inventariado aparezca como desconocido."""
        from apps.activos.models import Activo
        from apps.activos.services import generar_codigo_activo

        Activo.objects.create(
            codigo=generar_codigo_activo(Activo.Tipo.IMPRESORA), tipo=Activo.Tipo.IMPRESORA,
            farmacia=self.farmacia, unidad_negocio=self.sg, mac='d0:ad:08:58:61:65',
            estado=Activo.Estado.ASIGNADO,
        )
        resumen = self._sincronizar(self._parseadas())
        self.assertEqual(len(resumen['sin_declarar']), 3)

    def test_el_activo_declarado_se_puede_consultar_desde_el_dispositivo(self):
        from apps.activos.models import Activo
        from apps.activos.services import generar_codigo_activo
        from apps.monitoreo.models import DispositivoDetectado

        activo = Activo.objects.create(
            codigo=generar_codigo_activo(Activo.Tipo.IMPRESORA), tipo=Activo.Tipo.IMPRESORA,
            farmacia=self.farmacia, unidad_negocio=self.sg, mac='D0:AD:08:58:61:65',
            estado=Activo.Estado.ASIGNADO,
        )
        self._sincronizar(self._parseadas())

        dispositivo = DispositivoDetectado.objects.get(mac='D0:AD:08:58:61:65')
        self.assertEqual(dispositivo.activo_declarado, activo)
        otro = DispositivoDetectado.objects.get(ip='10.107.128.17')
        self.assertIsNone(otro.activo_declarado)

    def test_el_comando_informa_lo_no_inventariado(self):
        salida = io.StringIO()

        async def _falso(ip, comunidad, puerto):
            return self._parseadas()

        with patch('apps.monitoreo.mikrotik._leer_tabla_arp', _falso):
            call_command('descubrir_dispositivos_farmacia', '--farmacias', 'MCAR3', stdout=salida)

        texto = salida.getvalue()
        self.assertIn('Farmacias leídas: 1', texto)
        self.assertIn('SIN inventariar', texto)


class AdminMikrotikTests(TestCase):
    """Los dos modelos nuevos de Mikrotik en el admin.

    Hicieron falta porque se estaban recolectando datos que no tenían dónde mirarse: el
    sondeo guardaba identidad y dispositivos, y no existía ni una pantalla ni un registro
    en el admin. Un dato que nadie puede ver no sirve para nada.
    """

    def setUp(self):
        from apps.monitoreo.models import DispositivoDetectado, EquipoBordeFarmacia

        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia = Farmacia.objects.create(
            codigo='MCAR3', grupo=grupo, unidad_negocio=self.sg, ip_router='10.101.41.193',
        )
        EquipoBordeFarmacia.objects.create(
            farmacia=self.farmacia, modelo='RouterOS RB951Ui-2HnD', numero_serie='HEX094EM5FZ',
            version_routeros='6.49.17', nombre_sistema='MCAR3', uptime_segundos=6622664,
            ultima_lectura=timezone.now(),
        )
        DispositivoDetectado.objects.create(
            farmacia=self.farmacia, mac='D0:AD:08:58:61:65', ip='10.101.41.195',
            interfaz_indice=8, visto_por_ultima_vez=timezone.now(),
        )
        DispositivoDetectado.objects.create(
            farmacia=self.farmacia, mac='18:B6:F7:76:22:74', ip='10.101.41.205',
            interfaz_indice=8, visto_por_ultima_vez=timezone.now(),
        )

        self.admin_user = User.objects.create_superuser(
            username='u_admin_mk', password='x' * 16, email='a@b.c',
        )
        self.client.force_login(self.admin_user)

    def test_el_listado_de_equipos_de_borde_abre(self):
        resp = self.client.get('/admin/monitoreo/equipobordefarmacia/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'HEX094EM5FZ')
        self.assertContains(resp, '6.49.17')

    def test_el_uptime_se_muestra_en_dias(self):
        """6622664 segundos no le dice nada a nadie; 76,7 días sí."""
        resp = self.client.get('/admin/monitoreo/equipobordefarmacia/')
        self.assertContains(resp, '76.7')

    def test_el_listado_de_dispositivos_abre_y_cruza_con_lo_declarado(self):
        resp = self.client.get('/admin/monitoreo/dispositivodetectado/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '10.101.41.195')

    def test_el_cruce_no_hace_una_consulta_por_fila(self):
        """Se anota con un Exists: el admin muestra 100 por página y llamar a
        `activo_declarado` por fila serían 100 consultas por carga."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from apps.monitoreo.models import DispositivoDetectado

        with CaptureQueriesContext(connection) as ctx:
            self.client.get('/admin/monitoreo/dispositivodetectado/')
        con_dos = len(ctx.captured_queries)

        for i in range(20):
            DispositivoDetectado.objects.create(
                farmacia=self.farmacia, mac='AA:BB:CC:DD:EE:%02X' % i,
                ip='10.101.41.%d' % (100 + i), visto_por_ultima_vez=timezone.now(),
            )
        with CaptureQueriesContext(connection) as ctx:
            self.client.get('/admin/monitoreo/dispositivodetectado/')
        self.assertEqual(len(ctx.captured_queries), con_dos)

    def test_marca_como_declarado_el_que_tiene_un_activo_con_esa_mac(self):
        from apps.activos.models import Activo
        from apps.activos.services import generar_codigo_activo
        from apps.monitoreo.models import DispositivoDetectado

        Activo.objects.create(
            codigo=generar_codigo_activo(Activo.Tipo.IMPRESORA), tipo=Activo.Tipo.IMPRESORA,
            farmacia=self.farmacia, unidad_negocio=self.sg, mac='d0:ad:08:58:61:65',
            estado=Activo.Estado.ASIGNADO,
        )
        self.client.get('/admin/monitoreo/dispositivodetectado/')

        from django.db.models import Exists, OuterRef

        anotados = {
            d.mac: d.esta_declarado
            for d in DispositivoDetectado.objects.annotate(
                esta_declarado=Exists(Activo.objects.filter(
                    farmacia=OuterRef('farmacia'), mac__iexact=OuterRef('mac'),
                )),
            )
        }
        self.assertTrue(anotados['D0:AD:08:58:61:65'], 'no reconoció la MAC en minúscula')
        self.assertFalse(anotados['18:B6:F7:76:22:74'])

    def test_no_se_pueden_crear_a_mano(self):
        """Los escribe el sondeo SNMP: cargarlos a mano dejaría el panel afirmando algo
        que el equipo nunca dijo."""
        for ruta in ('equipobordefarmacia', 'dispositivodetectado'):
            resp = self.client.get('/admin/monitoreo/%s/add/' % ruta)
            self.assertIn(resp.status_code, (403, 302), ruta)


class DeteccionDeReinicioTests(TestCase):
    """Detección de reinicios del Mikrotik por caída del uptime.

    El caso real: el 15-sep-2026 se reinició GAT01 a mano y el historial siguió diciendo
    "sin caídas registradas". Era cierto —el sondeo de enlace exige tres fallas seguidas
    de ping, unos 6 minutos, y el equipo arrancó en menos— y aun así ocultaba lo que
    había pasado. El uptime pasó de 722 horas a 3,3 minutos y nadie lo veía porque la
    lectura solo ocurría cuando alguien corría el comando a mano.
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia = Farmacia.objects.create(
            codigo='GAT01', grupo=grupo, unidad_negocio=self.sg, ip_router='10.201.1.129',
        )

    def _leer(self, uptime, version='6.49.17'):
        from apps.monitoreo.mikrotik import sincronizar_identidad_equipos

        async def _falso(ip, comunidad, puerto):
            return {
                'modelo': 'RouterOS RB951Ui-2nD', 'uptime_segundos': uptime,
                'nombre_sistema': 'GAT01', 'version_routeros': version,
                'numero_serie': 'HH70A5GKB55',
            }

        with patch('apps.monitoreo.mikrotik._leer_identidad', _falso):
            return sincronizar_identidad_equipos([self.farmacia])

    def test_detecta_el_reinicio_cuando_el_uptime_baja(self):
        from apps.monitoreo.models import ReinicioEquipoBorde

        self._leer(2601839)          # 722 horas, como estaba GAT01
        resumen = self._leer(199)    # 3,3 minutos tras el reinicio

        self.assertEqual(ReinicioEquipoBorde.objects.count(), 1)
        reinicio = ReinicioEquipoBorde.objects.get()
        self.assertEqual(reinicio.farmacia, self.farmacia)
        self.assertAlmostEqual(reinicio.horas_encendido_antes, 722.7, places=1)
        self.assertEqual(len(resumen['reinicios']), 1)

    def test_un_uptime_que_sigue_creciendo_no_es_un_reinicio(self):
        from apps.monitoreo.models import ReinicioEquipoBorde

        self._leer(1000)
        self._leer(1900)
        self._leer(2800)
        self.assertEqual(ReinicioEquipoBorde.objects.count(), 0)

    def test_la_primera_lectura_nunca_cuenta_como_reinicio(self):
        """Sin una lectura anterior no hay contra qué comparar: dar por reiniciado a todo
        equipo recién incorporado llenaría el historial de eventos falsos."""
        from apps.monitoreo.models import ReinicioEquipoBorde

        self._leer(199)
        self.assertEqual(ReinicioEquipoBorde.objects.count(), 0)

    def test_guarda_el_arranque_estimado_y_no_el_momento_de_la_lectura(self):
        """Lo que importa es cuándo arrancó el equipo, no cuándo nos enteramos: entre las
        dos cosas puede pasar todo el intervalo del sondeo."""
        from django.utils import timezone

        from apps.monitoreo.models import ReinicioEquipoBorde

        self._leer(500000)
        antes = timezone.now()
        self._leer(600)  # arrancó hace 10 minutos

        reinicio = ReinicioEquipoBorde.objects.get()
        diferencia = (antes - reinicio.arranque_estimado).total_seconds()
        self.assertAlmostEqual(diferencia, 600, delta=15)

    def test_registra_la_version_para_distinguir_una_actualizacion(self):
        """Un arranque después de actualizar RouterOS no es lo mismo que uno espontáneo."""
        from apps.monitoreo.models import ReinicioEquipoBorde

        self._leer(500000, version='6.47.7')
        self._leer(120, version='6.49.17')
        self.assertEqual(ReinicioEquipoBorde.objects.get().version_routeros, '6.49.17')

    def test_dos_reinicios_generan_dos_eventos(self):
        """Varios seguidos con pocas horas entre medio son un equipo que se reinicia solo
        — el problema difícil de ver, porque entre reinicio y reinicio responde perfecto."""
        from apps.monitoreo.models import ReinicioEquipoBorde

        self._leer(90000)
        self._leer(300)
        self._leer(400)   # sigue subiendo, no es reinicio
        self._leer(100)   # se reinició otra vez
        self.assertEqual(ReinicioEquipoBorde.objects.count(), 2)

    def test_el_reinicio_no_ensucia_las_caidas_del_proveedor(self):
        """`EventoEnlaceFarmacia` es la evidencia para reclamarle a TELCONET: un reinicio
        que hicimos nosotros no puede aparecer ahí."""
        from apps.monitoreo.models import EventoEnlaceFarmacia

        self._leer(500000)
        self._leer(199)
        self.assertEqual(EventoEnlaceFarmacia.objects.filter(farmacia=self.farmacia).count(), 0)

    def test_el_modal_muestra_los_reinicios(self):
        from django.urls import reverse

        self._leer(2601839)
        self._leer(199)

        usuario = User.objects.create_user(username='u_reinicio', password='x')
        PerfilUsuario.objects.create(usuario=usuario, acceso_todas_unidades=True)
        usuario.user_permissions.add(
            Permission.objects.get(
                content_type__app_label='monitoreo', codename='view_estadoenlacefarmacia',
            ),
        )
        self.client.force_login(usuario)

        resp = self.client.get(reverse('panel:enlace_farmacia_modal', args=[self.farmacia.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Reinicios del equipo')
        self.assertContains(resp, 'no cuenta como caída del proveedor')


class PurgaMuestrasRedTests(TestCase):
    """Retención de `MuestraRedFarmacia`.

    Por qué existe: `muestra_metrica` y `evento_monitoreo` tenían purga e hypertable desde
    el principio; esta tabla quedó sin ninguna de las dos y nadie lo notó porque hoy solo
    4 Mikrotiks responden SNMP. Se escribe una fila por farmacia cada 5 minutos: con las
    700 respondiendo son ~6 millones de filas por mes. El momento de arreglarlo es ahora,
    mientras borrar es barato.
    """

    def setUp(self):
        grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia = Farmacia.objects.create(
            codigo='ML001', grupo=grupo, unidad_negocio=UnidadNegocio.objects.get(codigo='SG'),
        )

    def _muestra(self, dias_atras):
        from apps.monitoreo.models import MuestraRedFarmacia

        muestra = MuestraRedFarmacia.objects.create(
            farmacia=self.farmacia, bytes_recibidos=1, bytes_enviados=1,
        )
        # `timestamp` es auto_now_add, así que no se puede fijar al crear.
        MuestraRedFarmacia.objects.filter(pk=muestra.pk).update(
            timestamp=timezone.now() - timedelta(days=dias_atras),
        )
        return muestra

    def test_borra_las_mas_viejas_que_el_umbral(self):
        from apps.monitoreo.services import purgar_muestras_red_antiguas

        self._muestra(45)
        self._muestra(31)
        self.assertEqual(purgar_muestras_red_antiguas(dias=30), 2)

    def test_no_toca_las_recientes(self):
        from apps.monitoreo.models import MuestraRedFarmacia
        from apps.monitoreo.services import purgar_muestras_red_antiguas

        self._muestra(1)
        self._muestra(29)
        self._muestra(60)

        purgar_muestras_red_antiguas(dias=30)
        self.assertEqual(MuestraRedFarmacia.objects.count(), 2)

    def test_el_umbral_es_configurable(self):
        """Una farmacia con un reclamo abierto al proveedor puede necesitar más historial
        del que guarda la política por defecto."""
        from apps.monitoreo.models import MuestraRedFarmacia
        from apps.monitoreo.services import purgar_muestras_red_antiguas

        self._muestra(45)
        purgar_muestras_red_antiguas(dias=90)
        self.assertEqual(MuestraRedFarmacia.objects.count(), 1)

    def test_sin_muestras_viejas_no_borra_nada(self):
        from apps.monitoreo.services import purgar_muestras_red_antiguas

        self._muestra(1)
        self.assertEqual(purgar_muestras_red_antiguas(dias=30), 0)

    def test_la_tarea_de_celery_la_invoca(self):
        from apps.monitoreo.models import MuestraRedFarmacia
        from apps.monitoreo.tasks import purgar_muestras_red_task

        self._muestra(45)
        resultado = purgar_muestras_red_task()
        self.assertIn('1 muestra', resultado)
        self.assertEqual(MuestraRedFarmacia.objects.count(), 0)

    def test_esta_agendada_en_beat(self):
        """Sin la entrada en CELERY_BEAT_SCHEDULE la función existe y no la llama nadie —
        que es exactamente el estado del que venimos."""
        from django.conf import settings

        agendadas = {e['task'] for e in settings.CELERY_BEAT_SCHEDULE.values()}
        self.assertIn('apps.monitoreo.tasks.purgar_muestras_red_task', agendadas)


class SondeoDeActivosPorPingTests(TestCase):
    """Ping a los activos sin agente, hecho por el agente de su propia farmacia.

    Responde la pregunta que el inventario no podia: una impresora o un medianet existen
    como Activo, pero nadie sabia si estaban vivos. Cuando una caja llamaba diciendo "no
    imprime", no habia con que separar "esta apagado o desconectado" de "el POS no le
    habla".

    El sondeo sale del agente porque el servidor no tiene ruta hacia las IPs privadas de
    las farmacias -- el mismo motivo por el que el Mikrotik se sondea desde adentro.
    """

    def setUp(self):
        from apps.activos.models import Activo

        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia = Farmacia.objects.create(
            codigo='ML016', grupo=grupo, unidad_negocio=self.sg, ip_router='10.201.7.225',
        )
        self.estacion = Estacion.objects.create(
            codigo='ML016-A', farmacia=self.farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            estado_conexion=Estacion.EstadoConexion.ONLINE,
        )
        self.impresora = Activo.objects.create(
            codigo='CR-IMP-0001', tipo=Activo.Tipo.IMPRESORA, unidad_negocio=self.sg,
            farmacia=self.farmacia, ip='10.201.7.231',
        )

    # --- que se manda a pingear ---

    def test_incluye_los_activos_con_ip_de_esa_farmacia(self):
        from apps.monitoreo.services import objetivos_de_ping

        self.assertEqual(objetivos_de_ping(self.farmacia), '%d:10.201.7.231' % self.impresora.pk)

    def test_excluye_los_que_tienen_estacion_vinculada(self):
        """Esos ya reportan su estado por heartbeat. Pingearlos seria una segunda fuente
        de verdad sobre lo mismo, y cuando dos discrepan nadie sabe cual creer."""
        from apps.activos.models import Activo
        from apps.monitoreo.services import objetivos_de_ping

        Activo.objects.create(
            codigo='CR-DSK-0001', tipo=Activo.Tipo.DESKTOP, unidad_negocio=self.sg,
            farmacia=self.farmacia, estacion=self.estacion,
        )
        self.assertEqual(objetivos_de_ping(self.farmacia), '%d:10.201.7.231' % self.impresora.pk)

    def test_excluye_los_dados_de_baja(self):
        """Un equipo retirado no responde, y eso es correcto -- no una incidencia."""
        from apps.activos.models import Activo
        from apps.monitoreo.services import objetivos_de_ping

        Activo.objects.create(
            codigo='CR-IMP-0002', tipo=Activo.Tipo.IMPRESORA, unidad_negocio=self.sg,
            farmacia=self.farmacia, ip='10.201.7.232', estado=Activo.Estado.DADO_DE_BAJA,
        )
        self.assertNotIn('10.201.7.232', objetivos_de_ping(self.farmacia))

    def test_excluye_los_que_no_tienen_ip(self):
        from apps.activos.models import Activo
        from apps.monitoreo.services import objetivos_de_ping

        Activo.objects.create(
            codigo='CR-PIN-0001', tipo=Activo.Tipo.PINPAD, unidad_negocio=self.sg,
            farmacia=self.farmacia,
        )
        self.assertEqual(objetivos_de_ping(self.farmacia), '%d:10.201.7.231' % self.impresora.pk)

    def test_una_farmacia_sin_activos_pingeables_no_recibe_pedido(self):
        """Sin objetivos no se manda nada: hoy la mayoria de las farmacias no tienen IPs
        cargadas, y mandarles un pedido vacio seria trafico MQTT por nada."""
        from apps.activos.models import Activo
        from apps.monitoreo.services import solicitar_sondeo_activos_via_agente

        # Sin IP, no sin activo: el modelo prohibe borrar un Activo a proposito.
        Activo.objects.filter(pk=self.impresora.pk).update(ip=None)
        with patch('apps.catalogo.services.mqtt_publish.single') as mock_single:
            self.assertEqual(solicitar_sondeo_activos_via_agente(), 0)
        mock_single.assert_not_called()

    def test_se_pide_una_sola_vez_por_farmacia(self):
        """Con tres cajas en linea alcanza con que una pingee: son la misma LAN."""
        from apps.monitoreo.services import solicitar_sondeo_activos_via_agente

        for sufijo in ('B', 'C'):
            Estacion.objects.create(
                codigo='ML016-' + sufijo, farmacia=self.farmacia,
                estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
                estado_conexion=Estacion.EstadoConexion.ONLINE,
            )
        with patch('apps.catalogo.services.mqtt_publish.single') as mock_single:
            self.assertEqual(solicitar_sondeo_activos_via_agente(), 1)
        self.assertEqual(mock_single.call_count, 1)

    def test_el_comando_va_firmado(self):
        from apps.catalogo.services import enviar_consultar_activos_farmacia, firmar_payload

        with patch('apps.catalogo.services.mqtt_publish.single') as mock_single:
            enviar_consultar_activos_farmacia(self.estacion, '7:10.0.0.9')
        payload = json.loads(mock_single.call_args.args[1])
        self.assertEqual(
            payload['firma'],
            firmar_payload(
                comando='consultar_activos_farmacia', objetivos='7:10.0.0.9',
                estacion='ML016-A', timestamp=payload['timestamp'],
            ),
        )

    # --- que se guarda al volver ---

    def _reportar(self, resultados):
        from apps.monitoreo.services import registrar_estado_red_activos

        return registrar_estado_red_activos(estacion=self.estacion, resultados=resultados)

    def test_guarda_que_responde_con_su_latencia(self):
        from apps.monitoreo.models import EstadoRedActivo

        self._reportar([
            {'activo_id': self.impresora.pk, 'ip': '10.201.7.231', 'responde': True, 'latencia_ms': 3},
        ])
        estado = EstadoRedActivo.objects.get(activo=self.impresora)
        self.assertTrue(estado.responde)
        self.assertEqual(estado.latencia_ms, 3)
        self.assertIsNotNone(estado.ultima_respuesta)

    def test_guarda_tambien_que_no_responde(self):
        """A diferencia del sondeo de ancho de banda, aca la ausencia de respuesta ES el
        dato que se busca."""
        from apps.monitoreo.models import EstadoRedActivo

        self._reportar([{'activo_id': self.impresora.pk, 'ip': '10.201.7.231', 'responde': False}])
        estado = EstadoRedActivo.objects.get(activo=self.impresora)
        self.assertFalse(estado.responde)
        self.assertIsNone(estado.latencia_ms)
        self.assertIsNone(estado.ultima_respuesta)

    def test_una_caida_no_borra_el_ultimo_visto(self):
        """El dato que importa para decidir si hay que ir a la farmacia: hace cuanto esta
        caido. Pisar ultima_respuesta en cada verificacion lo destruiria."""
        from apps.monitoreo.models import EstadoRedActivo

        self._reportar([{'activo_id': self.impresora.pk, 'ip': '10.201.7.231', 'responde': True}])
        visto = EstadoRedActivo.objects.get(activo=self.impresora).ultima_respuesta

        self._reportar([{'activo_id': self.impresora.pk, 'ip': '10.201.7.231', 'responde': False}])
        estado = EstadoRedActivo.objects.get(activo=self.impresora)
        self.assertFalse(estado.responde)
        self.assertEqual(estado.ultima_respuesta, visto)

    def test_una_estacion_no_puede_reportar_activos_de_otra_farmacia(self):
        """El payload lo arma el agente: no puede ser autoridad sobre el inventario de
        una farmacia que no es la suya."""
        from apps.activos.models import Activo
        from apps.monitoreo.models import EstadoRedActivo

        otra = Farmacia.objects.create(
            codigo='ML099', grupo=self.farmacia.grupo, unidad_negocio=self.sg,
        )
        ajeno = Activo.objects.create(
            codigo='CR-IMP-0099', tipo=Activo.Tipo.IMPRESORA, unidad_negocio=self.sg,
            farmacia=otra, ip='10.99.0.1',
        )
        self.assertEqual(self._reportar([{'activo_id': ajeno.pk, 'responde': True}]), 0)
        self.assertFalse(EstadoRedActivo.objects.filter(activo=ajeno).exists())

    def test_horas_sin_responder_distingue_anoche_de_hace_una_semana(self):
        from apps.monitoreo.models import EstadoRedActivo

        self._reportar([{'activo_id': self.impresora.pk, 'ip': '10.201.7.231', 'responde': True}])
        estado = EstadoRedActivo.objects.get(activo=self.impresora)
        self.assertLess(estado.horas_sin_responder, 0.1)

        EstadoRedActivo.objects.filter(pk=estado.pk).update(
            ultima_respuesta=timezone.now() - timedelta(days=7), responde=False,
        )
        estado.refresh_from_db()
        self.assertAlmostEqual(estado.horas_sin_responder, 168, delta=1)

    def test_nunca_visto_no_es_lo_mismo_que_recien_caido(self):
        """None y "hace 0 horas" significan cosas opuestas: uno es "nunca contesto desde
        que lo monitoreamos" -- probablemente la IP esta mal -- y el otro "se acaba de
        caer"."""
        from apps.monitoreo.models import EstadoRedActivo

        self._reportar([{'activo_id': self.impresora.pk, 'ip': '10.201.7.231', 'responde': False}])
        self.assertIsNone(EstadoRedActivo.objects.get(activo=self.impresora).horas_sin_responder)

    def test_una_verificacion_vieja_no_se_lee_como_estado_actual(self):
        """Si la estacion que sondeaba se apago, el ultimo estado queda congelado. Sin
        este umbral, "responde: si" de hace tres dias se leeria como actual."""
        from apps.monitoreo.models import EstadoRedActivo

        self._reportar([{'activo_id': self.impresora.pk, 'ip': '10.201.7.231', 'responde': True}])
        estado = EstadoRedActivo.objects.get(activo=self.impresora)
        self.assertTrue(estado.verificacion_vigente)

        EstadoRedActivo.objects.filter(pk=estado.pk).update(
            ultima_verificacion=timezone.now() - timedelta(hours=5),
        )
        estado.refresh_from_db()
        self.assertFalse(estado.verificacion_vigente)

    def test_esta_agendada_en_beat(self):
        from django.conf import settings

        agendadas = {e['task'] for e in settings.CELERY_BEAT_SCHEDULE.values()}
        self.assertIn('apps.monitoreo.tasks.solicitar_sondeo_activos_task', agendadas)


class VerificarSaludTests(TestCase):
    """El comando que vigila al propio SAIDSOFT.

    Existe porque el sistema monitorea 700 farmacias y no habia nada monitoreandolo a el.
    Si el worker MQTT se cuelga sin morir, Docker lo reporta "corriendo" y el sintoma es
    ausencia de datos: se descubre tarde y por casualidad.

    Corre desde AFUERA del stack a proposito. Una alerta generada por Celery no sirve
    para avisar que Celery se cayo.
    """

    def _correr(self, *args):
        """Devuelve (texto, codigo_de_salida)."""
        salida = io.StringIO()
        try:
            call_command('verificar_salud', *args, stdout=salida, stderr=salida)
        except SystemExit as exc:
            return salida.getvalue(), exc.code
        return salida.getvalue(), 0

    def _latido(self, nombre, hace_segundos=0):
        from apps.mqtt_worker.models import WorkerHeartbeat

        WorkerHeartbeat.objects.update_or_create(
            nombre=nombre,
            defaults={'ultimo_latido': timezone.now() - timedelta(seconds=hace_segundos)},
        )

    def _sano(self):
        """Deja el sistema en estado sano: los dos workers latiendo hace un segundo."""
        self._latido('mqtt_worker', 1)
        self._latido('meshcentral_worker', 1)

    def test_con_todo_sano_sale_con_cero(self):
        self._sano()
        texto, codigo = self._correr()
        self.assertEqual(codigo, 0)
        self.assertIn('Todo sano', texto)

    def test_un_worker_atrasado_hace_fallar_el_comando(self):
        """El codigo de salida es el contrato: un temporizador externo avisa sin tener
        que interpretar el texto."""
        self._latido('mqtt_worker', 600)
        self._latido('meshcentral_worker', 1)
        texto, codigo = self._correr()
        self.assertEqual(codigo, 1)
        self.assertIn('mqtt_worker', texto)

    def test_un_worker_que_nunca_latio_tambien_falla(self):
        """Distinto de atrasado, y el mensaje lo dice: puede ser que el contenedor nunca
        haya arrancado, no que se haya colgado."""
        self._latido('mqtt_worker', 1)
        texto, codigo = self._correr()
        self.assertEqual(codigo, 1)
        self.assertIn('nunca registr', texto)

    def test_el_respaldo_sin_registro_no_es_un_problema(self):
        """Una instalacion nueva todavia no corrio ningun respaldo. Marcarlo como
        problema llenaria de ruido el primer dia de cualquier despliegue."""
        self._sano()
        texto, codigo = self._correr()
        self.assertEqual(codigo, 0)
        self.assertIn('instalaci', texto)

    def test_un_respaldo_viejo_si_es_un_problema(self):
        self._sano()
        self._latido('respaldo', 60 * 60 * 48)  # dos dias
        texto, codigo = self._correr()
        self.assertEqual(codigo, 1)
        self.assertIn('respaldo', texto)

    def test_modo_silencioso_solo_imprime_problemas(self):
        """Pensado para un temporizador que manda correo solo cuando hay salida."""
        self._sano()
        texto, codigo = self._correr('--silencioso')
        self.assertEqual(codigo, 0)
        self.assertEqual(texto.strip(), '')

    def test_los_umbrales_son_los_mismos_que_usa_el_panel(self):
        """Si la consola y la pantalla discreparan sobre que es "sano", una de las dos
        estaria mintiendo y no habria forma de saber cual."""
        from pathlib import Path

        from django.conf import settings

        from apps.monitoreo.management.commands import verificar_salud as cmd
        from apps.panel.views.dashboard import RESPALDO_UMBRAL_HORAS, WORKER_MQTT_UMBRAL_SEGUNDOS

        self.assertEqual(cmd.WORKER_MQTT_UMBRAL_SEGUNDOS, WORKER_MQTT_UMBRAL_SEGUNDOS)
        self.assertEqual(cmd.RESPALDO_UMBRAL_HORAS, RESPALDO_UMBRAL_HORAS)

        # Que coincidan hoy no alcanza: tienen que venir del MISMO lugar, o el dia que
        # alguien ajuste el umbral del panel la consola se queda con el viejo y las dos
        # afirman cosas distintas sin que nada falle.
        fuente = (Path(settings.BASE_DIR) / 'apps' / 'monitoreo' / 'management' / 'commands'
                  / 'verificar_salud.py').read_text(encoding='utf-8')
        self.assertIn('from apps.panel.views.dashboard import', fuente)

    def test_reporta_el_espacio_en_disco(self):
        """Por debajo del minimo, una purga o un respaldo pueden fallar a mitad de camino
        y dejar la base peor de lo que estaba."""
        self._sano()
        texto, _ = self._correr()
        self.assertIn('disco', texto)

    def test_solo_acota_a_un_componente(self):
        """Lo usa el healthcheck de cada contenedor: un contenedor tiene que medirse a sí
        mismo. Sin esto, un respaldo atrasado marcaría enfermo al worker MQTT y Docker lo
        reiniciaría en loop sin arreglar nada."""
        self._latido('mqtt_worker', 1)
        self._latido('respaldo', 60 * 60 * 48)  # respaldo viejo: problema del sistema

        texto, codigo = self._correr('--solo', 'mqtt_worker')
        self.assertEqual(codigo, 0, 'el worker está sano; el respaldo viejo no es asunto suyo')
        self.assertNotIn('respaldo', texto)

    def test_solo_sigue_detectando_el_problema_de_su_componente(self):
        self._latido('mqtt_worker', 600)
        texto, codigo = self._correr('--solo', 'mqtt_worker')
        self.assertEqual(codigo, 1)
        self.assertIn('mqtt_worker', texto)

    def test_un_componente_inexistente_falla_y_lista_los_validos(self):
        """Un nombre mal escrito en un healthcheck reportaría "sano" para siempre. Mejor
        que falle ruidoso y diga cuáles son los nombres reales."""
        self._sano()
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError) as ctx:
            self._correr('--solo', 'mqtt-worker')  # con guion, no con guion bajo
        self.assertIn('mqtt_worker', str(ctx.exception))


class SeedReglasAlertaTests(TestCase):
    """El juego inicial de reglas de alerta.

    Existe porque el motor de alertas esta entero y probado, y en produccion habia CERO
    reglas: el sistema detectaba y no avisaba. La capacidad existia sin estar en uso.

    Lo que define esta siembra no son los umbrales sino QUE se siembra apagado. Las
    farmacias cierran y apagan los equipos; una regla que alerte por ausencia dispararia
    todas las noches en cada local, y cientos de falsos positivos por noche terminan en
    que se ignoran todas, incluidas las verdaderas.
    """

    def setUp(self):
        User.objects.create_superuser(username='u_seed_reglas', password='x' * 16, email='a@b.c')

    def _correr(self, *args):
        salida = io.StringIO()
        call_command('seed_reglas_alerta', *args, stdout=salida)
        return salida.getvalue()

    def test_por_defecto_simula(self):
        from apps.monitoreo.models import ReglaAlerta

        texto = self._correr()
        self.assertIn('Simulacion', texto)
        self.assertEqual(ReglaAlerta.objects.count(), 0)

    def test_crea_el_juego_completo(self):
        from apps.monitoreo.models import ReglaAlerta

        self._correr('--aplicar')
        self.assertEqual(ReglaAlerta.objects.count(), 12)

    def test_las_reglas_que_se_disparan_por_ausencia_nacen_apagadas(self):
        """El punto entero del comando. `sin_heartbeat` y `agente_caido_red_viva` se
        evaluan cuando NO llega un reporte, asi que una farmacia cerrada las dispara."""
        from apps.monitoreo.models import Metrica, ReglaAlerta

        self._correr('--aplicar')
        por_ausencia = ReglaAlerta.objects.filter(
            metrica__in=[Metrica.SIN_HEARTBEAT, Metrica.AGENTE_CAIDO_RED_VIVA],
        )
        self.assertEqual(por_ausencia.count(), 2)
        self.assertFalse(por_ausencia.filter(activo=True).exists())

    def test_las_reglas_de_metrica_nacen_activas(self):
        """Son seguras: `evaluar_reglas_metricas` solo corre cuando llega una muestra, y
        una estacion apagada no manda ninguna. No pueden dispararse de noche."""
        from apps.monitoreo.models import Metrica, ReglaAlerta

        self._correr('--aplicar')
        de_metrica = ReglaAlerta.objects.exclude(
            metrica__in=[Metrica.SIN_HEARTBEAT, Metrica.AGENTE_CAIDO_RED_VIVA],
        )
        # Las dos de reloj entran acá: se evalúan con el latido, no por ausencia,
        # así que una farmacia cerrada tampoco las dispara.
        self.assertEqual(de_metrica.count(), 10)
        self.assertEqual(de_metrica.filter(activo=True).count(), 10)

    def test_ninguna_abre_mantenimiento_automatico(self):
        """Activar una regla no puede empezar a generar ordenes de trabajo sin que nadie
        lo haya decidido — mismo criterio que el default del modelo."""
        from apps.monitoreo.models import ReglaAlerta

        self._correr('--aplicar')
        self.assertFalse(ReglaAlerta.objects.filter(abre_mantenimiento=True).exists())

    def test_todas_son_globales(self):
        """Sin unidad de negocio aplican a los tres clientes. Sembrarlas por cliente
        multiplicaria por tres el mantenimiento de los mismos umbrales."""
        from apps.monitoreo.models import ReglaAlerta

        self._correr('--aplicar')
        self.assertEqual(ReglaAlerta.objects.filter(unidad_negocio__isnull=True).count(), 12)

    def test_correrlo_dos_veces_no_duplica(self):
        from apps.monitoreo.models import ReglaAlerta

        self._correr('--aplicar')
        texto = self._correr('--aplicar')
        self.assertEqual(ReglaAlerta.objects.count(), 12)
        self.assertIn('intactas', texto)

    def test_no_pisa_un_umbral_afinado_a_mano(self):
        """Si alguien bajo un umbral porque conoce su parque, sabe algo que el comando
        no."""
        from apps.monitoreo.models import ReglaAlerta

        self._correr('--aplicar')
        regla = ReglaAlerta.objects.get(nombre='CPU saturada (90%)')
        regla.umbral = 70
        regla.save(update_fields=['umbral'])

        self._correr('--aplicar')
        regla.refresh_from_db()
        self.assertEqual(regla.umbral, 70)

    def test_con_actualizar_si_ajusta_los_umbrales(self):
        from apps.monitoreo.models import ReglaAlerta

        self._correr('--aplicar')
        ReglaAlerta.objects.filter(nombre='CPU saturada (90%)').update(umbral=70)

        self._correr('--aplicar', '--actualizar')
        self.assertEqual(ReglaAlerta.objects.get(nombre='CPU saturada (90%)').umbral, 90)

    def test_actualizar_no_vuelve_a_apagar_lo_que_alguien_encendio(self):
        """Si se activo `sin_heartbeat` despues de resolver el tema de los horarios,
        volver a apagarla seria deshacer una decision tomada."""
        from apps.monitoreo.models import ReglaAlerta

        self._correr('--aplicar')
        ReglaAlerta.objects.filter(nombre='Sin heartbeat (30 min)').update(activo=True)

        self._correr('--aplicar', '--actualizar')
        self.assertTrue(ReglaAlerta.objects.get(nombre='Sin heartbeat (30 min)').activo)

    def test_siembra_las_dos_reglas_de_servicios_del_POS(self):
        """Dos reglas para la misma metrica no es redundancia: evaluar_regla_servicio_pos
        elige cual aplicar segun el campo `critico` del servicio. Con una sola, la caida de
        Odoo y la de la base local abririan la misma alerta, y "critica" dejaria de
        significar "anda a la farmacia"."""
        from apps.monitoreo.models import Metrica, ReglaAlerta

        self._correr('--aplicar')
        reglas = ReglaAlerta.objects.filter(metrica=Metrica.SERVICIO_POS_CAIDO)
        self.assertEqual(reglas.count(), 2)
        self.assertEqual(
            sorted(reglas.values_list('severidad', flat=True)),
            [ReglaAlerta.Severidad.CRITICAL, ReglaAlerta.Severidad.WARNING],
        )
        self.assertEqual(reglas.filter(activo=True).count(), 2)

    def test_las_reglas_de_servicios_del_POS_son_las_que_el_motor_usa(self):
        """Que existan no alcanza: si la severidad no coincidiera con la que
        evaluar_regla_servicio_pos busca, la regla quedaria decorativa."""
        from apps.catalogo.models import Estacion, Farmacia, Grupo
        from apps.monitoreo.models import Alerta, EstadoServicioPos, ReglaAlerta, UnidadNegocio
        from apps.monitoreo.services import evaluar_regla_servicio_pos

        self._correr('--aplicar')
        sg = UnidadNegocio.objects.get(codigo='SG')
        farmacia = Farmacia.objects.create(
            codigo='ML001', grupo=Grupo.objects.create(codigo='TRX001'), unidad_negocio=sg,
        )
        estacion = Estacion.objects.create(codigo='ML001-A', farmacia=farmacia)

        critico = EstadoServicioPos.objects.create(
            estacion=estacion, servicio='pg_local', disponible=False, critico=True,
            ultima_verificacion=timezone.now(),
            # Respondia antes: sin esto seria un servicio que nunca existio, que el motor
            # ignora a proposito (ver EstadoServicioPos.nunca_respondio).
            ultima_respuesta=timezone.now() - timedelta(hours=1),
        )
        evaluar_regla_servicio_pos(estacion, critico)
        self.assertEqual(
            Alerta.objects.get().regla.severidad, ReglaAlerta.Severidad.CRITICAL,
            'la caida de la base local tiene que abrir la regla critica',
        )

    def test_sin_superusuario_falla_con_un_mensaje_util(self):
        from django.core.management.base import CommandError

        User.objects.all().delete()
        with self.assertRaises(CommandError) as ctx:
            self._correr('--aplicar')
        self.assertIn('createsuperuser', str(ctx.exception))

    def test_las_reglas_sembradas_son_evaluables(self):
        """Que una regla exista no significa que el motor la entienda: si la metrica no
        coincide con un campo de MuestraMetrica, `evaluar_reglas_metricas` la saltea en
        silencio y la regla no sirve para nada."""
        from apps.monitoreo.models import Metrica, MuestraMetrica, ReglaAlerta

        self._correr('--aplicar')
        # Se usa hasattr y no `_meta.get_fields()` porque el motor hace
        # `getattr(muestra, regla.metrica)`: varias metricas son propiedades calculadas
        # (disco_usado_pct sale de libre/total), no columnas. Mirar la lista de campos
        # daria un falso negativo sobre reglas que funcionan perfecto.
        aparte = {Metrica.SIN_HEARTBEAT, Metrica.AGENTE_CAIDO_RED_VIVA,
                  Metrica.BITLOCKER_DESHABILITADO, Metrica.POS_ERRORES,
                  Metrica.SERVICIO_POS_CAIDO, Metrica.DESFASE_RELOJ}
        for regla in ReglaAlerta.objects.exclude(metrica__in=aparte):
            self.assertTrue(
                hasattr(MuestraMetrica, regla.metrica),
                '"%s" usa la metrica %s, que MuestraMetrica no expone: el motor la '
                'saltearia en silencio' % (regla.nombre, regla.metrica),
            )


class ServiciosPosTests(TestCase):
    """Los servicios externos de los que depende el POS para vender.

    Cuando una caja no puede vender, la pregunta es donde se corta la cadena: la base
    local, el nodo central, Odoo o el web service de recargas. Averiguarlo exigia entrar
    al equipo. Esto lo pregunta desde la propia estacion cada pocos minutos.

    Se mide desde la ESTACION y no desde el servidor central a proposito: lo que importa
    no es si el servicio esta vivo en abstracto, sino si ESA caja lo alcanza. Una base
    central sana con la ruta rota desde una farmacia es, para esa farmacia, una base
    caida, y desde el servidor se veria perfecta.
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia = Farmacia.objects.create(
            codigo='ML001', grupo=grupo, unidad_negocio=self.sg,
        )
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=self.farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self.admin = User.objects.create_superuser(username='u_svc_pos', password='x' * 16)

    def _resultado(self, servicio='pg_local', disponible=True, **extra):
        base = {
            'servicio': servicio,
            'disponible': disponible,
            'latencia_ms': 42 if disponible else None,
            'mensaje': 'PostgreSQL 160006' if disponible else 'connection refused',
            'endpoint': '10.0.0.5:5432/pos',
            'critico': servicio == 'pg_local',
        }
        base.update(extra)
        return base

    def _registrar(self, *resultados):
        from apps.monitoreo.services import registrar_servicios_pos

        return registrar_servicios_pos(estacion=self.estacion, resultados=list(resultados))

    def _caer(self, *resultados):
        """Deja los servicios CAIDOS de verdad: primero responden, despues dejan de hacerlo.

        El chequeo exitoso previo no es decorativo. Desde el 18-sep-2026 un servicio que
        nunca respondio NO abre alerta (ver EstadoServicioPos.nunca_respondio): eso es
        configuracion vieja, no una caida. Sin este paso, el fixture describiria un
        servicio que jamas existio y el motor tendria razon en ignorarlo.
        """
        self._registrar(*[dict(r, disponible=True, latencia_ms=42) for r in resultados])
        return self._registrar(*[dict(r, disponible=False, latencia_ms=None) for r in resultados])

    def _regla(self, severidad):
        from apps.monitoreo.models import Metrica, ReglaAlerta

        return ReglaAlerta.objects.create(
            nombre='Servicio del POS caido (%s)' % severidad,
            metrica=Metrica.SERVICIO_POS_CAIDO, severidad=severidad,
            umbral=0, duracion_minutos=0, creado_por=self.admin,
        )

    # --- lo que se guarda ---

    def test_guarda_un_estado_por_servicio(self):
        from apps.monitoreo.models import EstadoServicioPos

        self.assertEqual(
            self._registrar(self._resultado('pg_local'), self._resultado('odoo')), 2,
        )
        self.assertEqual(EstadoServicioPos.objects.count(), 2)

    def test_el_segundo_reporte_sobrescribe_y_no_acumula(self):
        """Es estado actual, no historial: el historial de latencia vive en
        MuestraServicioPos."""
        from apps.monitoreo.models import EstadoServicioPos

        self._registrar(self._resultado('pg_local'))
        self._registrar(self._resultado('pg_local', latencia_ms=99))
        self.assertEqual(EstadoServicioPos.objects.count(), 1)
        self.assertEqual(EstadoServicioPos.objects.get().latencia_ms, 99)

    def test_una_caida_no_borra_el_ultimo_visto(self):
        """El dato que decide si hay que ir a la farmacia: hace cuanto esta caido."""
        from apps.monitoreo.models import EstadoServicioPos

        self._registrar(self._resultado('pg_local'))
        visto = EstadoServicioPos.objects.get().ultima_respuesta

        self._registrar(self._resultado('pg_local', disponible=False))
        estado = EstadoServicioPos.objects.get()
        self.assertFalse(estado.disponible)
        self.assertEqual(estado.ultima_respuesta, visto)
        self.assertIsNone(estado.latencia_ms, 'una latencia de un chequeo fallido no es un dato')

    def test_solo_guarda_muestra_de_latencia_cuando_responde(self):
        """Graficar un cero diria que contesto instantaneamente."""
        from apps.monitoreo.models import MuestraServicioPos

        self._registrar(self._resultado('pg_local'))
        self._registrar(self._resultado('pg_local', disponible=False))
        self.assertEqual(MuestraServicioPos.objects.count(), 1)

    def test_ignora_un_servicio_desconocido(self):
        """El payload lo arma el agente: no puede inventar servicios que el modelo no
        conoce."""
        from apps.monitoreo.models import EstadoServicioPos

        self.assertEqual(self._registrar(self._resultado('minado_de_bitcoin')), 0)
        self.assertFalse(EstadoServicioPos.objects.exists())

    def test_una_verificacion_vieja_no_se_lee_como_estado_actual(self):
        """Si la estacion se apaga, su ultimo chequeo queda congelado y diria "todo bien"
        indefinidamente."""
        from apps.monitoreo.models import EstadoServicioPos

        self._registrar(self._resultado('pg_local'))
        estado = EstadoServicioPos.objects.get()
        self.assertTrue(estado.verificacion_vigente)

        EstadoServicioPos.objects.filter(pk=estado.pk).update(
            ultima_verificacion=timezone.now() - timedelta(hours=5),
        )
        estado.refresh_from_db()
        self.assertFalse(estado.verificacion_vigente)

    # --- alertas ---

    def test_un_servicio_critico_caido_abre_alerta_critica(self):
        from apps.monitoreo.models import Alerta, ReglaAlerta

        regla = self._regla(ReglaAlerta.Severidad.CRITICAL)
        self._caer(self._resultado('pg_local'))

        alerta = Alerta.objects.get(regla=regla, estacion=self.estacion)
        self.assertEqual(alerta.estado, Alerta.Estado.ABIERTA)

    def test_un_servicio_no_critico_no_dispara_la_regla_critica(self):
        """Sin Odoo la caja sigue vendiendo. Tratar las dos caidas igual haria que una
        alerta critica deje de significar "anda a la farmacia"."""
        from apps.monitoreo.models import Alerta, ReglaAlerta

        self._regla(ReglaAlerta.Severidad.CRITICAL)
        self._registrar(self._resultado('odoo', disponible=False, critico=False))
        self.assertFalse(Alerta.objects.exists())

    def test_un_servicio_no_critico_dispara_la_regla_de_advertencia(self):
        from apps.monitoreo.models import Alerta, ReglaAlerta

        regla = self._regla(ReglaAlerta.Severidad.WARNING)
        self._caer(self._resultado('odoo', critico=False))
        self.assertTrue(Alerta.objects.filter(regla=regla, estado=Alerta.Estado.ABIERTA).exists())

    def test_al_volver_el_servicio_la_alerta_se_resuelve_sola(self):
        from apps.monitoreo.models import Alerta, ReglaAlerta

        self._regla(ReglaAlerta.Severidad.CRITICAL)
        self._caer(self._resultado('pg_local'))
        self.assertTrue(Alerta.objects.filter(estado=Alerta.Estado.ABIERTA).exists())

        self._registrar(self._resultado('pg_local', disponible=True))
        self.assertFalse(Alerta.objects.filter(estado=Alerta.Estado.ABIERTA).exists())

    def test_un_servicio_que_vuelve_no_resuelve_la_alerta_si_otro_sigue_caido(self):
        """Visto en produccion el 17-sep-2026: Odoo sin responder en ML017-B y la alerta
        figurando RESUELTA, porque pg_central habia contestado despues y comparte regla
        con el.

        La alerta es por (regla, estacion) y los tres servicios no criticos comparten
        regla. Resolver por el que acaba de volver deja la alerta cerrada con otro todavia
        caido — que es peor que no tener alerta: afirma que se arreglo algo que sigue mal.
        """
        from apps.monitoreo.models import Alerta, ReglaAlerta

        self._regla(ReglaAlerta.Severidad.WARNING)
        self._caer(
            self._resultado('odoo', critico=False),
            self._resultado('pg_central', critico=False),
        )
        self.assertTrue(Alerta.objects.filter(estado=Alerta.Estado.ABIERTA).exists())

        # Vuelve uno solo: la alerta tiene que seguir abierta.
        self._registrar(self._resultado('pg_central', disponible=True, critico=False))
        self.assertTrue(
            Alerta.objects.filter(estado=Alerta.Estado.ABIERTA).exists(),
            'Odoo sigue caido: la alerta no puede darse por resuelta',
        )

        # Vuelven todos: recien ahi se resuelve.
        self._registrar(self._resultado('odoo', disponible=True, critico=False))
        self.assertFalse(Alerta.objects.filter(estado=Alerta.Estado.ABIERTA).exists())

    def test_un_no_critico_caido_no_deja_abierta_la_alerta_critica(self):
        """Las dos familias se evaluan por separado: que Odoo este caido no puede sostener
        abierta la alerta de la base local."""
        from apps.monitoreo.models import Alerta, ReglaAlerta

        self._regla(ReglaAlerta.Severidad.CRITICAL)
        self._regla(ReglaAlerta.Severidad.WARNING)
        self._caer(
            self._resultado('pg_local'),
            self._resultado('odoo', critico=False),
        )
        self.assertEqual(Alerta.objects.filter(estado=Alerta.Estado.ABIERTA).count(), 2)

        self._registrar(self._resultado('pg_local', disponible=True))
        abiertas = Alerta.objects.filter(estado=Alerta.Estado.ABIERTA)
        self.assertEqual(abiertas.count(), 1)
        self.assertEqual(abiertas.get().regla.severidad, ReglaAlerta.Severidad.WARNING)

    def test_dos_chequeos_caidos_seguidos_no_abren_dos_alertas(self):
        """La alerta es por condicion, no por evento: si no, un servicio caido una hora
        generaria doce alertas identicas."""
        from apps.monitoreo.models import Alerta, ReglaAlerta

        self._regla(ReglaAlerta.Severidad.CRITICAL)
        self._caer(self._resultado('pg_local'))
        self._registrar(self._resultado('pg_local', disponible=False))
        self.assertEqual(Alerta.objects.count(), 1)

    def test_sin_regla_configurada_no_se_inventa_ninguna_alerta(self):
        from apps.monitoreo.models import Alerta

        self._registrar(self._resultado('pg_local', disponible=False))
        self.assertFalse(Alerta.objects.exists())

    def test_una_regla_de_otra_unidad_de_negocio_no_aplica(self):
        from apps.monitoreo.models import Alerta, Metrica, ReglaAlerta

        ReglaAlerta.objects.create(
            nombre='Solo para MIA', metrica=Metrica.SERVICIO_POS_CAIDO,
            severidad=ReglaAlerta.Severidad.CRITICAL, umbral=0, duracion_minutos=0,
            unidad_negocio=UnidadNegocio.objects.get(codigo='MIA'), creado_por=self.admin,
        )
        self._registrar(self._resultado('pg_local', disponible=False))
        self.assertFalse(Alerta.objects.exists())

    # --- el camino completo desde MQTT ---

    def test_el_mensaje_mqtt_crea_el_estado(self):
        from apps.monitoreo.models import EstadoServicioPos
        from apps.mqtt_worker.services import manejar_servicios_pos

        manejar_servicios_pos('ML001-A', {
            'token': self.estacion.token_enrolamiento,
            'resultados': [self._resultado('pg_local'), self._resultado('recargas_soap')],
        })
        self.assertEqual(EstadoServicioPos.objects.count(), 2)

    def test_un_token_invalido_no_escribe_nada(self):
        from apps.monitoreo.models import EstadoServicioPos
        from apps.mqtt_worker.services import manejar_servicios_pos

        manejar_servicios_pos('ML001-A', {
            'token': 'token-inventado', 'resultados': [self._resultado()],
        })
        self.assertFalse(EstadoServicioPos.objects.exists())

    def test_un_reporte_sin_resultados_no_marca_todo_como_caido(self):
        """Significa que el agente no pudo leer el .exe.Config. Marcar los cuatro
        servicios como caidos convertiria un problema de lectura en una falla aparente de
        toda la infraestructura."""
        from apps.monitoreo.models import EstadoServicioPos
        from apps.mqtt_worker.services import manejar_servicios_pos

        manejar_servicios_pos('ML001-A', {
            'token': self.estacion.token_enrolamiento, 'resultados': [],
        })
        self.assertFalse(EstadoServicioPos.objects.exists())

    # --- que no se filtren credenciales ---

    def test_el_modelo_no_tiene_donde_guardar_una_contrasena(self):
        """La defensa de fondo: aunque el agente mandara la clave, no hay campo que la
        reciba. Mismo criterio que EstadoRedActivo, que guarda la IP sondeada y nada mas.
        """
        from apps.monitoreo.models import EstadoServicioPos

        campos = {f.name for f in EstadoServicioPos._meta.get_fields()}
        for prohibido in ('password', 'contrasena', 'clave', 'usuario', 'credencial'):
            self.assertNotIn(prohibido, campos)

    def test_lo_que_se_guarda_del_endpoint_no_incluye_credenciales(self):
        from apps.monitoreo.models import EstadoServicioPos

        self._registrar(self._resultado('pg_local', endpoint='10.0.0.5:5432/pos'))
        estado = EstadoServicioPos.objects.get()
        self.assertEqual(estado.endpoint, '10.0.0.5:5432/pos')
        self.assertNotIn('@', estado.endpoint, 'una URL con user:pass@host filtraria la clave')


class ComposeMeshCentralTests(TestCase):
    """El servicio que EJECUTA sincronizar_meshcentral_task tiene que recibir la
    configuracion de MeshCentral.

    Existe por un fallo silencioso encontrado en produccion (17-sep-2026): las
    MESHCENTRAL_API_* estaban en el .env y en el servicio meshcentral_worker, pero no en
    celery_worker, que es donde corre la tarea periodica. `configurado()` daba False y la
    tarea devolvia '0 nodo(s) sincronizado(s)' cada 15 minutos -- indistinguible en el log
    de un resync legitimo sin novedades. El efecto real estaba dos saltos mas alla: sin
    ese resync los EstadoDispositivo envejecian por encima de FRESCURA_MESHCENTRAL_MINUTOS
    y evaluar_cruce_monitoreo los descartaba, dejando la regla agente_caido_red_viva sin
    disparar nunca. Nada fallaba; simplemente la alerta no existia.

    Se parsea el YAML como texto a proposito: pyyaml no es dependencia del proyecto y no
    vale agregarla para una sola verificacion.
    """

    VARIABLES = ('MESHCENTRAL_API_WS_URL', 'MESHCENTRAL_API_USUARIO', 'MESHCENTRAL_API_PASSWORD')

    def _entorno_del_servicio(self, servicio):
        from django.conf import settings

        ruta = settings.BASE_DIR / 'deploy' / 'docker-compose.yml'
        lineas = ruta.read_text(encoding='utf-8').splitlines()
        dentro_servicio = dentro_entorno = False
        entorno = []
        for linea in lineas:
            if linea.startswith(f'  {servicio}:'):
                dentro_servicio = True
                continue
            if dentro_servicio and linea.startswith('  ') and not linea.startswith('   ') and linea.strip():
                break  # empezo el siguiente servicio
            if not dentro_servicio:
                continue
            if linea.startswith('    environment:'):
                dentro_entorno = True
                continue
            if dentro_entorno:
                if linea.strip() and not linea.startswith('      '):
                    dentro_entorno = False
                    continue
                if ':' in linea and not linea.strip().startswith('#'):
                    entorno.append(linea.split(':', 1)[0].strip())
        self.assertTrue(entorno, f'no se pudo leer el environment de {servicio} en docker-compose.yml')
        return entorno

    def test_la_tarea_periodica_esta_programada(self):
        """Si alguien saca la tarea del schedule, este test deja de tener sentido y avisa."""
        from django.conf import settings

        tareas = {e['task'] for e in settings.CELERY_BEAT_SCHEDULE.values()}
        self.assertIn('apps.monitoreo.tasks.sincronizar_meshcentral_task', tareas)

    def test_celery_worker_recibe_la_configuracion_de_meshcentral(self):
        entorno = self._entorno_del_servicio('celery_worker')
        for variable in self.VARIABLES:
            self.assertIn(
                variable, entorno,
                f'{variable} falta en celery_worker: sincronizar_meshcentral_task corre ahi y sin '
                f'esto devuelve 0 nodos en silencio',
            )

    def test_meshcentral_worker_sigue_recibiendola(self):
        entorno = self._entorno_del_servicio('meshcentral_worker')
        for variable in self.VARIABLES:
            self.assertIn(variable, entorno)


def _fijar_umbral_aviso(minutos):
    """El umbral vive en ConfiguracionMonitoreo, no en settings: override_settings no lo
    alcanza. Se usa en los tests que prueban el ENVIO y no el umbral en si."""
    from apps.monitoreo.models import ConfiguracionMonitoreo

    config = ConfiguracionMonitoreo.obtener()
    config.minutos_minimos_aviso_enlace = minutos
    config.save()
    return config


@override_settings(ENLACES_NOTIFICAR_A=['redes@ejemplo.com'])
class NotificarCambiosEnlacesTests(TestCase):
    """El aviso proactivo de enlaces caidos/recuperados (lo que se reporta al proveedor)."""

    def setUp(self):
        from apps.monitoreo.models import EventoEnlaceFarmacia

        # Misma cadena de fixtures que el resto del archivo: la unidad 'SG' la crea una
        # migracion de datos, el grupo no cuelga de la unidad y la farmacia si.
        self.unidad = UnidadNegocio.objects.get(codigo='SG')
        self.grupo = Grupo.objects.create(codigo='TRX901')
        self.f1 = Farmacia.objects.create(
            codigo='TSTE01', grupo=self.grupo, unidad_negocio=self.unidad, ip_router='192.168.61.1',
            circuito_proveedor='telconet61-avejoseurbina',
        )
        self.f2 = Farmacia.objects.create(
            codigo='TSTE02', grupo=self.grupo, unidad_negocio=self.unidad, ip_router='192.169.185.1',
            circuito_proveedor='puntonet-colimes',
        )
        self.Evento = EventoEnlaceFarmacia
        _fijar_umbral_aviso(0)  # estos prueban el envio, no el umbral
        mail.outbox = []

    def _caida(self, farmacia, *, minutos_atras=10, fin=None, **kwargs):
        return self.Evento.objects.create(
            farmacia=farmacia,
            inicio=timezone.now() - timedelta(minutes=minutos_atras),
            fin=fin,
            circuito_proveedor=farmacia.circuito_proveedor,
            **kwargs,
        )

    def test_una_caida_nueva_manda_un_correo_y_marca_el_evento(self):
        from apps.monitoreo.enlaces import notificar_cambios_enlaces

        evento = self._caida(self.f1)
        resumen = notificar_cambios_enlaces()

        self.assertEqual(resumen['caidos'], 1)
        self.assertTrue(resumen['enviado'])
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('TSTE01', mail.outbox[0].body)
        evento.refresh_from_db()
        self.assertIsNotNone(evento.notificado_en)

    def test_no_se_avisa_dos_veces_de_la_misma_caida(self):
        """Lo que hace usable el aviso: sin esto, cada corrida (cada 5 min) repetiria
        las 162 caidas abiertas hasta que alguien las arregle."""
        from apps.monitoreo.enlaces import notificar_cambios_enlaces

        self._caida(self.f1)
        notificar_cambios_enlaces()
        mail.outbox = []

        resumen = notificar_cambios_enlaces()
        self.assertEqual(resumen['caidos'], 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_varias_caidas_van_en_un_solo_correo_agrupadas_por_proveedor(self):
        from apps.monitoreo.enlaces import notificar_cambios_enlaces

        self._caida(self.f1)
        self._caida(self.f2)
        notificar_cambios_enlaces()

        self.assertEqual(len(mail.outbox), 1, 'un correo por caida no escala a 196 caidas diarias')
        cuerpo = mail.outbox[0].body
        self.assertIn('telconet61', cuerpo)
        self.assertIn('puntonet', cuerpo)
        self.assertIn('TSTE01', cuerpo)
        self.assertIn('TSTE02', cuerpo)

    def test_la_recuperacion_tambien_avisa_y_dice_cuanto_duro(self):
        from apps.monitoreo.enlaces import notificar_cambios_enlaces

        evento = self._caida(self.f1, minutos_atras=45, notificado_en=timezone.now())
        evento.fin = timezone.now()
        evento.save(update_fields=['fin'])

        resumen = notificar_cambios_enlaces()
        self.assertEqual(resumen['recuperados'], 1)
        self.assertIn('RECUPERADOS', mail.outbox[0].body)
        self.assertIn('45 min', mail.outbox[0].body)
        evento.refresh_from_db()
        self.assertIsNotNone(evento.recuperacion_notificada_en)

    def test_sin_novedades_no_manda_correo(self):
        from apps.monitoreo.enlaces import notificar_cambios_enlaces

        self._caida(self.f1, notificado_en=timezone.now())
        resumen = notificar_cambios_enlaces()

        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(resumen['enviado'])

    @override_settings(ENLACES_NOTIFICAR_A=[])
    def test_sin_destinatarios_no_manda_nada_ni_marca_el_evento(self):
        """Que no haya a quien avisarle no debe consumir el aviso: cuando se configure
        el destinatario, la caida que sigue abierta tiene que poder avisarse."""
        from apps.monitoreo.enlaces import notificar_cambios_enlaces

        evento = self._caida(self.f1)
        resumen = notificar_cambios_enlaces()

        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(resumen['enviado'])
        evento.refresh_from_db()
        self.assertIsNone(evento.notificado_en, 'el evento tiene que quedar pendiente de aviso')

    def test_la_tarea_esta_programada(self):
        from django.conf import settings

        tareas = {e['task'] for e in settings.CELERY_BEAT_SCHEDULE.values()}
        self.assertIn('apps.monitoreo.tasks.notificar_cambios_enlaces_task', tareas)


class NuncaRespondioTests(TestCase):
    """Separar "nunca respondio" de "se cayo".

    El panel decia 162 caidos cuando lo accionable eran 28: 133 farmacias nunca habian
    respondido un sondeo, todas desde el instante en que arranco el monitoreo. Eso no es
    una caida que reportar -- el proveedor responde que su enlace esta arriba.
    """

    def setUp(self):
        self.unidad = UnidadNegocio.objects.get(codigo='SG')
        self.grupo = Grupo.objects.create(codigo='TRX902')
        self.farmacia = Farmacia.objects.create(
            codigo='TSTN01', grupo=self.grupo, unidad_negocio=self.unidad, ip_router='10.0.0.9',
        )

    def test_una_farmacia_que_nunca_respondio_no_cuenta_como_caida(self):
        from apps.monitoreo.enlaces import registrar_sondeo
        from apps.monitoreo.models import EstadoEnlaceFarmacia

        for _ in range(EstadoEnlaceFarmacia.UMBRAL_FALLAS_CONSECUTIVAS):
            registrar_sondeo(self.farmacia, False, None)

        estado = EstadoEnlaceFarmacia.objects.get(farmacia=self.farmacia)
        self.assertFalse(estado.alcanzable)
        self.assertFalse(estado.respondio_alguna_vez)
        self.assertTrue(estado.nunca_respondio)

    def test_si_respondio_una_vez_una_caida_posterior_si_es_caida(self):
        from apps.monitoreo.enlaces import registrar_sondeo
        from apps.monitoreo.models import EstadoEnlaceFarmacia

        registrar_sondeo(self.farmacia, True, 12.0)
        for _ in range(EstadoEnlaceFarmacia.UMBRAL_FALLAS_CONSECUTIVAS):
            registrar_sondeo(self.farmacia, False, None)

        estado = EstadoEnlaceFarmacia.objects.get(farmacia=self.farmacia)
        self.assertFalse(estado.alcanzable)
        self.assertTrue(estado.respondio_alguna_vez)
        self.assertFalse(estado.nunca_respondio, 'respondio antes: esto SI es una caida')

    @override_settings(ENLACES_NOTIFICAR_A=['redes@ejemplo.com'])
    def test_no_se_le_reporta_al_proveedor_un_enlace_que_nunca_estuvo_arriba(self):
        """El punto entero del cambio."""
        from apps.monitoreo.enlaces import notificar_cambios_enlaces, registrar_sondeo
        from apps.monitoreo.models import EstadoEnlaceFarmacia, EventoEnlaceFarmacia

        mail.outbox = []
        for _ in range(EstadoEnlaceFarmacia.UMBRAL_FALLAS_CONSECUTIVAS):
            registrar_sondeo(self.farmacia, False, None)
        self.assertTrue(EventoEnlaceFarmacia.objects.filter(farmacia=self.farmacia).exists(),
                        'el evento se registra igual: el historico no se pierde')

        resumen = notificar_cambios_enlaces()
        self.assertEqual(resumen['caidos'], 0)
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(ENLACES_NOTIFICAR_A=['redes@ejemplo.com'])
    def test_una_caida_real_si_se_avisa(self):
        from apps.monitoreo.enlaces import notificar_cambios_enlaces, registrar_sondeo
        from apps.monitoreo.models import EstadoEnlaceFarmacia

        _fijar_umbral_aviso(0)  # lo que se prueba acá es el contraste, no el umbral
        mail.outbox = []
        registrar_sondeo(self.farmacia, True, 12.0)
        for _ in range(EstadoEnlaceFarmacia.UMBRAL_FALLAS_CONSECUTIVAS):
            registrar_sondeo(self.farmacia, False, None)

        resumen = notificar_cambios_enlaces()
        self.assertEqual(resumen['caidos'], 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('TSTN01', mail.outbox[0].body)

    def test_el_panel_las_cuenta_aparte_y_no_como_caidas(self):
        from apps.monitoreo.enlaces import registrar_sondeo
        from apps.monitoreo.models import EstadoEnlaceFarmacia

        otra = Farmacia.objects.create(
            codigo='TSTN02', grupo=self.grupo, unidad_negocio=self.unidad, ip_router='10.0.0.10',
        )
        registrar_sondeo(otra, True, 10.0)  # esta si respondio alguna vez
        for _ in range(EstadoEnlaceFarmacia.UMBRAL_FALLAS_CONSECUTIVAS):
            registrar_sondeo(self.farmacia, False, None)
            registrar_sondeo(otra, False, None)

        usuario = User.objects.create_user(username='panel_enlaces', password='x', is_superuser=True,
                                           is_staff=True)
        self.client.force_login(usuario)
        respuesta = self.client.get(reverse('panel:enlaces_farmacias_lista'))

        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.context['caidas'], 1, 'solo la que alguna vez respondio')
        self.assertEqual(respuesta.context['nunca_respondieron'], 1)
        self.assertContains(respuesta, 'Nunca respondió')


class ComposeCorreoTests(ComposeMeshCentralTests):
    """Los servicios que mandan correo tienen que recibir la config de SMTP.

    Mismo fallo que ComposeMeshCentralTests documenta, repetido: verificado en el
    servidor el 17-sep-2026, las EMAIL_* no estaban en NINGUN servicio del compose ni
    en el .env. `send_mail` intentaba autenticarse contra smtp.gmail.com con usuario y
    clave vacios y fallaba; como todo el proyecto usa fail_silently=True (para que un
    SMTP caido no tumbe la ingesta de metricas), no quedaba ni un rastro. Ninguna
    notificacion por correo habia salido nunca, y la alerta se veia igual de bien en el
    panel.

    Hereda el parser de ComposeMeshCentralTests a proposito: si el formato del compose
    cambia, un solo lugar que arreglar.
    """

    # web: notificar_alerta desde una vista. worker: desde la ingesta MQTT.
    # celery_worker: las tareas periodicas (escalar_alertas_abiertas, enlaces).
    SERVICIOS_QUE_MANDAN_CORREO = ('web', 'worker', 'celery_worker')
    VARIABLES_SMTP = ('EMAIL_HOST', 'EMAIL_HOST_USER', 'EMAIL_HOST_PASSWORD')

    def test_los_tres_servicios_que_mandan_correo_tienen_smtp(self):
        for servicio in self.SERVICIOS_QUE_MANDAN_CORREO:
            entorno = self._entorno_del_servicio(servicio)
            for variable in self.VARIABLES_SMTP:
                self.assertIn(
                    variable, entorno,
                    f'{variable} falta en {servicio}: send_mail fallaria en silencio',
                )

    def test_celery_worker_recibe_a_quien_avisarle_de_enlaces(self):
        """notificar_cambios_enlaces_task corre ahi; sin esto no manda nada y lo unico
        que queda es una linea de log."""
        self.assertIn('ENLACES_NOTIFICAR_A', self._entorno_del_servicio('celery_worker'))

    def test_la_tarea_de_enlaces_esta_programada(self):
        from django.conf import settings

        tareas = {e['task'] for e in settings.CELERY_BEAT_SCHEDULE.values()}
        self.assertIn('apps.monitoreo.tasks.notificar_cambios_enlaces_task', tareas)


class ResolverWanPorNexthopTests(TestCase):
    """Resolver la interfaz WAN en routers que no exponen la ipRouteTable clasica.

    GCH20 (10.201.6.33) tenia SNMP bien configurado —respondia sysName con su
    community— pero salia "sin SNMP" en el panel: su RouterOS devuelve noSuchName para
    la ipRouteTable de RFC1213, asi que no habia forma de saber cual era la WAN.

    Los datos de estos tests son los que devolvieron los routers reales el 17-sep-2026,
    no inventados: por eso el test sirve de regresion si alguien toca el parseo.
    """

    # GCH20: la ruta por defecto sale por el nexthop 10.107.130.73; el router tiene
    # 10.107.130.74/30 en ifIndex 4 (ether3_TELCO) y 10.201.6.33/27 en ifIndex 9.
    RUTAS_GCH20 = [('1.3.6.1.2.1.4.24.4.1.5.0.0.0.0.0.0.0.0.0.10.107.130.73', 0)]
    IFINDEX_GCH20 = [
        ('1.3.6.1.2.1.4.20.1.2.10.107.130.74', 4),
        ('1.3.6.1.2.1.4.20.1.2.10.201.6.33', 9),
    ]
    MASCARAS_GCH20 = [
        ('1.3.6.1.2.1.4.20.1.3.10.107.130.74', '255.255.255.252'),
        ('1.3.6.1.2.1.4.20.1.3.10.201.6.33', '255.255.255.224'),
    ]

    def _walk_falso(self, rutas=None, indices=None, mascaras=None):
        from apps.monitoreo import mikrotik

        async def _walk(engine, comunidad, target, raiz):
            if raiz == mikrotik._OID_IP_CIDR_ROUTE_IF_INDEX:
                return self.RUTAS_GCH20 if rutas is None else rutas
            if raiz == mikrotik._OID_IP_AD_ENT_IF_INDEX:
                return self.IFINDEX_GCH20 if indices is None else indices
            if raiz == mikrotik._OID_IP_AD_ENT_NETMASK:
                return self.MASCARAS_GCH20 if mascaras is None else mascaras
            return []
        return _walk

    def _resolver(self, **kwargs):
        import asyncio

        from apps.monitoreo import mikrotik

        with patch.object(mikrotik, '_walk', self._walk_falso(**kwargs)):
            return asyncio.run(mikrotik._resolver_wan_por_nexthop(None, '10.201.6.33', 'gch20', None))

    def test_elige_la_interfaz_cuya_subred_contiene_el_nexthop(self):
        self.assertEqual(self._resolver(), 4, 'ether3_TELCO es la WAN, no la interfaz de la LAN')

    def test_sin_ruta_por_defecto_no_inventa_una_interfaz(self):
        """Una tabla de rutas sin la ruta 0.0.0.0 no permite deducir nada."""
        otras = [('1.3.6.1.2.1.4.24.4.1.5.10.101.0.0.255.255.0.0.0.10.107.130.73', 0)]
        self.assertIsNone(self._resolver(rutas=otras))

    def test_si_ninguna_subred_contiene_el_nexthop_devuelve_none(self):
        ajenas = [('1.3.6.1.2.1.4.20.1.2.192.168.5.1', 7)]
        mascaras = [('1.3.6.1.2.1.4.20.1.3.192.168.5.1', '255.255.255.0')]
        self.assertIsNone(self._resolver(indices=ajenas, mascaras=mascaras))

    def test_la_mascara_se_lee_venga_como_texto_o_como_bytes(self):
        from apps.monitoreo.mikrotik import _mascara_a_entero

        esperado = 0xFFFFFFFC

        class ComoIpAddress:
            def prettyPrint(self):
                return '255.255.255.252'

        self.assertEqual(_mascara_a_entero(ComoIpAddress()), esperado)
        self.assertEqual(_mascara_a_entero('255.255.255.252'), esperado)
        self.assertEqual(_mascara_a_entero(b'\xff\xff\xff\xfc'), esperado)
        self.assertIsNone(_mascara_a_entero(None))

    def test_ip_a_entero_rechaza_lo_que_no_es_una_ip(self):
        from apps.monitoreo.mikrotik import _ip_a_entero

        self.assertEqual(_ip_a_entero('10.107.130.73'), (10 << 24) | (107 << 16) | (130 << 8) | 73)
        for invalida in ('10.107.130', '10.107.130.999', 'ether3', ''):
            self.assertIsNone(_ip_a_entero(invalida), invalida)


@override_settings(TELEGRAM_BOT_TOKEN='TOKEN-SECRETO-DE-PRUEBA')
class TelegramNotificacionTests(TestCase):
    """Telegram enganchado al motor de alertas que ya existe, no un bot aparte."""

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        grupo = Grupo.objects.create(codigo='TRX910')
        farmacia = Farmacia.objects.create(codigo='TSTT01', grupo=grupo, unidad_negocio=self.sg)
        self.estacion = Estacion.objects.create(
            codigo='TSTT01-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self.usuario = User.objects.create_user(username='u_tg', password='x')
        self.regla_critica = ReglaAlerta.objects.create(
            nombre='Servicio critico caido', metrica=Metrica.SERVICIO_POS_CAIDO,
            umbral=0, severidad=ReglaAlerta.Severidad.CRITICAL, creado_por=self.usuario,
        )
        self.regla_warning = ReglaAlerta.objects.create(
            nombre='CPU alta', metrica=Metrica.CPU_CARGA_PCT, umbral=90,
            severidad=ReglaAlerta.Severidad.WARNING, creado_por=self.usuario,
        )
        self.canal = CanalNotificacion.objects.create(
            tipo=CanalNotificacion.Tipo.TELEGRAM, destino='-1001234567890',
            creado_por=self.usuario,
        )

    def _cuerpos_enviados(self, urlopen):
        """El JSON de cada POST que se le paso a urlopen."""
        cuerpos = []
        for llamada in urlopen.call_args_list:
            req = llamada.args[0]
            cuerpos.append(json.loads(req.data.decode('utf-8')))
        return cuerpos

    # --- enganche en notificar_alerta ---

    def test_una_alerta_critica_llega_a_telegram_con_circulo_rojo(self):
        alerta = Alerta.objects.create(
            regla=self.regla_critica, estacion=self.estacion, valor_disparador=0)
        with patch('apps.monitoreo.services.urllib.request.urlopen') as urlopen:
            notificar_alerta(alerta)
        cuerpos = self._cuerpos_enviados(urlopen)
        self.assertEqual(len(cuerpos), 1)
        self.assertEqual(cuerpos[0]['chat_id'], '-1001234567890')
        self.assertTrue(cuerpos[0]['text'].startswith('🔴'), cuerpos[0]['text'][:40])
        self.assertIn('TSTT01-A', cuerpos[0]['text'])

    def test_una_alerta_warning_llega_con_circulo_amarillo(self):
        alerta = Alerta.objects.create(
            regla=self.regla_warning, estacion=self.estacion, valor_disparador=95)
        with patch('apps.monitoreo.services.urllib.request.urlopen') as urlopen:
            notificar_alerta(alerta)
        self.assertTrue(self._cuerpos_enviados(urlopen)[0]['text'].startswith('🟡'))

    def test_sin_token_no_se_llama_a_la_api(self):
        """Vacio = desactivado, sin romper el resto de la notificacion."""
        alerta = Alerta.objects.create(
            regla=self.regla_critica, estacion=self.estacion, valor_disparador=0)
        with override_settings(TELEGRAM_BOT_TOKEN=''):
            with patch('apps.monitoreo.services.urllib.request.urlopen') as urlopen:
                notificar_alerta(alerta)
        urlopen.assert_not_called()

    def test_canal_de_otra_unidad_no_recibe_nada(self):
        self.canal.unidad_negocio = self.mia
        self.canal.save(update_fields=['unidad_negocio'])
        alerta = Alerta.objects.create(
            regla=self.regla_critica, estacion=self.estacion, valor_disparador=0)
        with patch('apps.monitoreo.services.urllib.request.urlopen') as urlopen:
            notificar_alerta(alerta)
        urlopen.assert_not_called()

    def test_canal_inactivo_no_recibe_nada(self):
        self.canal.activo = False
        self.canal.save(update_fields=['activo'])
        alerta = Alerta.objects.create(
            regla=self.regla_critica, estacion=self.estacion, valor_disparador=0)
        with patch('apps.monitoreo.services.urllib.request.urlopen') as urlopen:
            notificar_alerta(alerta)
        urlopen.assert_not_called()

    def test_telegram_caido_no_rompe_la_notificacion(self):
        alerta = Alerta.objects.create(
            regla=self.regla_critica, estacion=self.estacion, valor_disparador=0)
        with patch('apps.monitoreo.services.urllib.request.urlopen',
                   side_effect=urllib.error.URLError('caido')):
            notificar_alerta(alerta)  # no debe lanzar

    def test_la_escalada_tambien_llega_a_telegram(self):
        """Punto 4 de la verificacion: escalar_alertas_abiertas pasa por notificar_alerta."""
        from apps.monitoreo.services import escalar_alertas_abiertas

        alerta = Alerta.objects.create(
            regla=self.regla_critica, estacion=self.estacion, valor_disparador=0)
        Alerta.objects.filter(pk=alerta.pk).update(
            abierta_en=timezone.now() - timedelta(minutes=90))

        with patch('apps.monitoreo.services.urllib.request.urlopen') as urlopen:
            escaladas = escalar_alertas_abiertas()

        self.assertEqual(escaladas, 1)
        textos = [c['text'] for c in self._cuerpos_enviados(urlopen)]
        self.assertTrue(any('SIN ATENDER' in t for t in textos), textos)

    # --- el secreto no se filtra ---

    def test_el_token_no_aparece_en_el_mensaje_ni_en_el_log(self):
        """Punto 6 de la verificacion. El token va en la URL, nunca en el texto ni en
        un log: un log se comparte, se sube a un ticket y se pega en un chat."""
        alerta = Alerta.objects.create(
            regla=self.regla_critica, estacion=self.estacion, valor_disparador=0)

        with patch('apps.monitoreo.services.urllib.request.urlopen',
                   side_effect=urllib.error.URLError('caido')):
            with self.assertLogs('apps.monitoreo.services', level='WARNING') as registro:
                notificar_alerta(alerta)
        self.assertNotIn('TOKEN-SECRETO-DE-PRUEBA', '\n'.join(registro.output))

        with patch('apps.monitoreo.services.urllib.request.urlopen') as urlopen:
            notificar_alerta(alerta)
        for cuerpo in self._cuerpos_enviados(urlopen):
            self.assertNotIn('TOKEN-SECRETO-DE-PRUEBA', json.dumps(cuerpo))

    def test_el_token_no_queda_guardado_en_la_base(self):
        campos = {f.name for f in CanalNotificacion._meta.get_fields()}
        for prohibido in ('token', 'api_key', 'secreto', 'password'):
            self.assertNotIn(prohibido, campos)


@override_settings(TELEGRAM_BOT_TOKEN='TOKEN-SECRETO-DE-PRUEBA', ANTHROPIC_API_KEY='CLAVE-IA-DE-PRUEBA')
class DiagnosticoIATests(TestCase):
    """Diagnostico automatico: solo criticas, una vez por incidente, marcado como IA.

    Nunca se llama a la API real: se mockea `generar_diagnostico` o el propio cliente.
    Una suite que dependiera de una API paga seria lenta, no reproducible y costaria
    dinero en cada corrida.
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX911')
        self.farmacia = Farmacia.objects.create(codigo='TSTI01', grupo=grupo, unidad_negocio=self.sg)
        self.estacion = Estacion.objects.create(
            codigo='TSTI01-A', farmacia=self.farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self.usuario = User.objects.create_user(username='u_ia', password='x')
        self.critica = ReglaAlerta.objects.create(
            nombre='Servicio critico del POS sin responder', metrica=Metrica.SERVICIO_POS_CAIDO,
            umbral=0, severidad=ReglaAlerta.Severidad.CRITICAL, creado_por=self.usuario,
        )
        self.warning = ReglaAlerta.objects.create(
            nombre='CPU alta', metrica=Metrica.CPU_CARGA_PCT, umbral=90,
            severidad=ReglaAlerta.Severidad.WARNING, creado_por=self.usuario,
        )
        CanalNotificacion.objects.create(
            tipo=CanalNotificacion.Tipo.TELEGRAM, destino='-100999', creado_por=self.usuario,
        )

    # --- puntos 1 y 2 de la verificacion: quien dispara IA y quien no ---

    def test_una_alerta_critica_encola_el_diagnostico(self):
        from apps.monitoreo.services import abrir_o_mantener_alerta

        with patch('apps.monitoreo.tasks.diagnosticar_alerta_task.delay') as encolar:
            with patch('apps.monitoreo.services.urllib.request.urlopen'):
                alerta = abrir_o_mantener_alerta(self.critica, self.estacion, 0)
        encolar.assert_called_once_with(alerta.pk)

    def test_una_alerta_warning_no_llama_a_la_api_de_ia(self):
        """Punto 2: WARNING notifica por Telegram pero NO dispara diagnostico."""
        from apps.monitoreo.services import abrir_o_mantener_alerta

        with patch('apps.monitoreo.tasks.diagnosticar_alerta_task.delay') as encolar:
            with patch('apps.monitoreo.services.urllib.request.urlopen') as urlopen:
                abrir_o_mantener_alerta(self.warning, self.estacion, 95)
        encolar.assert_not_called()
        self.assertTrue(urlopen.called, 'la WARNING si tiene que llegar a Telegram')

    def test_sin_api_key_no_se_encola_nada(self):
        from apps.monitoreo.services import abrir_o_mantener_alerta

        with override_settings(ANTHROPIC_API_KEY=''):
            with patch('apps.monitoreo.tasks.diagnosticar_alerta_task.delay') as encolar:
                with patch('apps.monitoreo.services.urllib.request.urlopen'):
                    abrir_o_mantener_alerta(self.critica, self.estacion, 0)
        encolar.assert_not_called()

    def test_un_broker_caido_no_impide_abrir_ni_notificar_la_alerta(self):
        from apps.monitoreo.services import abrir_o_mantener_alerta

        with patch('apps.monitoreo.tasks.diagnosticar_alerta_task.delay',
                   side_effect=OSError('redis caido')):
            with patch('apps.monitoreo.services.urllib.request.urlopen') as urlopen:
                alerta = abrir_o_mantener_alerta(self.critica, self.estacion, 0)
        self.assertIsNotNone(alerta, 'la alerta es el producto; el diagnostico es un extra')
        self.assertTrue(urlopen.called)

    # --- punto 3: una sola vez por incidente ---

    def test_un_segundo_reporte_del_mismo_problema_no_vuelve_a_pedir_diagnostico(self):
        from apps.monitoreo.services import abrir_o_mantener_alerta

        with patch('apps.monitoreo.tasks.diagnosticar_alerta_task.delay') as encolar:
            with patch('apps.monitoreo.services.urllib.request.urlopen'):
                abrir_o_mantener_alerta(self.critica, self.estacion, 0)
                abrir_o_mantener_alerta(self.critica, self.estacion, 0)
                abrir_o_mantener_alerta(self.critica, self.estacion, 0)
        self.assertEqual(encolar.call_count, 1, 'control de costo: una llamada por incidente')

    def test_el_task_no_regenera_si_ya_habia_diagnostico(self):
        """Defensa contra un doble encolado (retry de Celery, corrida manual)."""
        from apps.monitoreo.tasks import diagnosticar_alerta_task

        alerta = Alerta.objects.create(
            regla=self.critica, estacion=self.estacion, valor_disparador=0,
            diagnostico_ia='ya estaba', diagnostico_generado_en=timezone.now(),
        )
        with patch('apps.monitoreo.diagnostico_ia.generar_diagnostico') as generar:
            resultado = diagnosticar_alerta_task(alerta.pk)
        generar.assert_not_called()
        self.assertIn('Ya se', resultado)

    def test_el_task_rechaza_una_warning_aunque_lo_llamen_a_mano(self):
        from apps.monitoreo.tasks import diagnosticar_alerta_task

        alerta = Alerta.objects.create(regla=self.warning, estacion=self.estacion, valor_disparador=95)
        with patch('apps.monitoreo.diagnostico_ia.generar_diagnostico') as generar:
            diagnosticar_alerta_task(alerta.pk)
        generar.assert_not_called()

    # --- el mensaje de seguimiento ---

    def test_el_diagnostico_se_guarda_y_se_manda_marcado_como_automatico(self):
        from apps.monitoreo.tasks import diagnosticar_alerta_task

        alerta = Alerta.objects.create(regla=self.critica, estacion=self.estacion, valor_disparador=0)
        with patch('apps.monitoreo.diagnostico_ia.generar_diagnostico',
                   return_value='CAUSA MAS PROBABLE: el servidor del POS no responde.'):
            with patch('apps.monitoreo.services.urllib.request.urlopen') as urlopen:
                diagnosticar_alerta_task(alerta.pk)

        alerta.refresh_from_db()
        self.assertIn('CAUSA MAS PROBABLE', alerta.diagnostico_ia)
        self.assertIsNotNone(alerta.diagnostico_generado_en)

        enviado = json.loads(urlopen.call_args.args[0].data.decode('utf-8'))['text']
        self.assertIn('\U0001f916', enviado)
        self.assertIn('automatico (IA)'.replace('automatico', 'automático'), enviado)
        self.assertIn('confirmar antes de actuar', enviado)

    def test_si_la_api_falla_se_marca_el_intento_y_no_se_manda_nada(self):
        """No reintentar en el mismo incidente: el contexto no cambio, solo el costo."""
        from apps.monitoreo.tasks import diagnosticar_alerta_task

        alerta = Alerta.objects.create(regla=self.critica, estacion=self.estacion, valor_disparador=0)
        with patch('apps.monitoreo.diagnostico_ia.generar_diagnostico', return_value=None):
            with patch('apps.monitoreo.services.urllib.request.urlopen') as urlopen:
                diagnosticar_alerta_task(alerta.pk)
        alerta.refresh_from_db()
        self.assertIsNone(alerta.diagnostico_ia)
        self.assertIsNotNone(alerta.diagnostico_generado_en)
        urlopen.assert_not_called()

    # --- el contexto que se le manda al modelo ---

    def test_el_contexto_incluye_los_servicios_del_pos_medidos(self):
        from apps.monitoreo.diagnostico_ia import _contexto_de
        from apps.monitoreo.models import EstadoServicioPos, ServicioPos

        EstadoServicioPos.objects.create(
            estacion=self.estacion, servicio=ServicioPos.PG_LOCAL, disponible=False,
            mensaje='connection timeout', endpoint='192.168.111.6:5433/hub', critico=True,
            ultima_verificacion=timezone.now(),
        )
        alerta = Alerta.objects.create(regla=self.critica, estacion=self.estacion, valor_disparador=0)
        contexto = _contexto_de(alerta)
        self.assertIn('connection timeout', contexto)
        self.assertIn('192.168.111.6:5433/hub', contexto)
        self.assertIn('NO responde', contexto)

    def test_el_contexto_no_incluye_ningun_secreto(self):
        """Punto 6: ni el token ni la API key pueden viajar a la API de Claude."""
        from apps.monitoreo.diagnostico_ia import _contexto_de

        alerta = Alerta.objects.create(regla=self.critica, estacion=self.estacion, valor_disparador=0)
        contexto = _contexto_de(alerta)
        self.assertNotIn('TOKEN-SECRETO-DE-PRUEBA', contexto)
        self.assertNotIn('CLAVE-IA-DE-PRUEBA', contexto)

    def test_el_prompt_prohibe_inventar_una_causa(self):
        """Lo que separa un diagnostico util de una adivinanza con formato."""
        from apps.monitoreo.diagnostico_ia import _INSTRUCCIONES

        self.assertIn('No inventes datos', _INSTRUCCIONES)
        self.assertIn('no alcanza para concluir', _INSTRUCCIONES)

    def test_sin_api_key_generar_diagnostico_no_intenta_nada(self):
        from apps.monitoreo.diagnostico_ia import generar_diagnostico

        alerta = Alerta.objects.create(regla=self.critica, estacion=self.estacion, valor_disparador=0)
        with override_settings(ANTHROPIC_API_KEY=''):
            self.assertIsNone(generar_diagnostico(alerta))

    def test_la_api_key_no_queda_en_el_log_si_la_llamada_falla(self):
        from apps.monitoreo.diagnostico_ia import generar_diagnostico

        alerta = Alerta.objects.create(regla=self.critica, estacion=self.estacion, valor_disparador=0)
        with patch('anthropic.Anthropic', side_effect=RuntimeError('401 con CLAVE-IA-DE-PRUEBA')):
            with self.assertLogs('apps.monitoreo.diagnostico_ia', level='WARNING') as registro:
                self.assertIsNone(generar_diagnostico(alerta))
        self.assertNotIn('CLAVE-IA-DE-PRUEBA', '\n'.join(registro.output))


@override_settings(TELEGRAM_BOT_TOKEN='TOKEN-SECRETO-DE-PRUEBA', ENLACES_TELEGRAM_CHAT_ID='-100777')
class EnlacesTelegramTests(TestCase):
    """Punto 5: el resumen agrupado de enlaces tambien sale por Telegram, no solo por correo."""

    def setUp(self):
        from apps.monitoreo.models import EstadoEnlaceFarmacia

        self.unidad = UnidadNegocio.objects.get(codigo='SG')
        self.grupo = Grupo.objects.create(codigo='TRX912')
        self.farmacia = Farmacia.objects.create(
            codigo='TSTG01', grupo=self.grupo, unidad_negocio=self.unidad, ip_router='10.0.2.1',
            circuito_proveedor='telconet-pruebas',
        )
        self.UMBRAL = EstadoEnlaceFarmacia.UMBRAL_FALLAS_CONSECUTIVAS
        _fijar_umbral_aviso(0)  # estos prueban el envio por Telegram, no el umbral
        mail.outbox = []

    def _caer(self):
        from apps.monitoreo.enlaces import registrar_sondeo

        registrar_sondeo(self.farmacia, True, 12.0)
        for _ in range(self.UMBRAL):
            registrar_sondeo(self.farmacia, False, None)

    def _texto_enviado(self, urlopen):
        return json.loads(urlopen.call_args.args[0].data.decode('utf-8'))['text']

    def test_una_caida_llega_a_telegram(self):
        from apps.monitoreo.enlaces import notificar_cambios_enlaces

        self._caer()
        with patch('apps.monitoreo.services.urllib.request.urlopen') as urlopen:
            resumen = notificar_cambios_enlaces()

        self.assertEqual(resumen['caidos'], 1)
        urlopen.assert_called_once()
        texto = self._texto_enviado(urlopen)
        self.assertIn('TSTG01', texto)
        self.assertIn('telconet', texto)

    def test_la_recuperacion_tambien_llega(self):
        from apps.monitoreo.enlaces import notificar_cambios_enlaces, registrar_sondeo

        self._caer()
        with patch('apps.monitoreo.services.urllib.request.urlopen'):
            notificar_cambios_enlaces()
        registrar_sondeo(self.farmacia, True, 15.0)

        with patch('apps.monitoreo.services.urllib.request.urlopen') as urlopen:
            resumen = notificar_cambios_enlaces()
        self.assertEqual(resumen['recuperados'], 1)
        self.assertIn('RECUPERADOS', self._texto_enviado(urlopen))

    def test_solo_con_telegram_configurado_igual_avisa_y_marca_el_evento(self):
        """Sin correo pero con chat: el aviso tiene que salir igual, no perderse."""
        from apps.monitoreo.enlaces import notificar_cambios_enlaces
        from apps.monitoreo.models import EventoEnlaceFarmacia

        self._caer()
        with override_settings(ENLACES_NOTIFICAR_A=[]):
            with patch('apps.monitoreo.services.urllib.request.urlopen') as urlopen:
                resumen = notificar_cambios_enlaces()

        self.assertTrue(resumen['enviado'])
        urlopen.assert_called_once()
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(
            EventoEnlaceFarmacia.objects.filter(notificado_en__isnull=True).exists(),
            'el evento ya fue avisado por Telegram: no puede quedar pendiente',
        )

    def test_sin_ningun_canal_no_marca_el_evento_como_avisado(self):
        """Ni correo ni Telegram: el aviso queda pendiente para cuando se configure uno."""
        from apps.monitoreo.enlaces import notificar_cambios_enlaces
        from apps.monitoreo.models import EventoEnlaceFarmacia

        self._caer()
        with override_settings(ENLACES_NOTIFICAR_A=[], ENLACES_TELEGRAM_CHAT_ID=''):
            with patch('apps.monitoreo.services.urllib.request.urlopen') as urlopen:
                resumen = notificar_cambios_enlaces()

        self.assertFalse(resumen['enviado'])
        urlopen.assert_not_called()
        self.assertTrue(EventoEnlaceFarmacia.objects.filter(notificado_en__isnull=True).exists())

    def test_el_token_no_viaja_en_el_texto_del_resumen(self):
        from apps.monitoreo.enlaces import notificar_cambios_enlaces

        self._caer()
        with patch('apps.monitoreo.services.urllib.request.urlopen') as urlopen:
            notificar_cambios_enlaces()
        self.assertNotIn('TOKEN-SECRETO-DE-PRUEBA', self._texto_enviado(urlopen))


class ComposeTelegramIATests(ComposeMeshCentralTests):
    """Las variables nuevas tienen que llegar al servicio que las usa.

    Tercera vez que aparece la misma clase de fallo en el proyecto (MESHCENTRAL_*,
    EMAIL_*): config presente en el .env pero ausente en el contenedor, y la
    funcionalidad muerta en silencio. Por eso cada variable nueva entra con su test.
    """

    def test_los_tres_servicios_que_notifican_tienen_el_token_de_telegram(self):
        for servicio in ('web', 'worker', 'celery_worker'):
            self.assertIn('TELEGRAM_BOT_TOKEN', self._entorno_del_servicio(servicio), servicio)

    def test_la_api_key_esta_donde_se_decide_encolar_y_donde_se_ejecuta(self):
        """`_pedir_diagnostico_ia` mira la clave en el proceso que ABRE la alerta (web o
        el worker MQTT); el task la usa en celery_worker. Si falta en los primeros, no se
        encola nunca y la funcion queda muerta aunque celery_worker si la tenga."""
        for servicio in ('web', 'worker', 'celery_worker'):
            self.assertIn('ANTHROPIC_API_KEY', self._entorno_del_servicio(servicio), servicio)

    def test_el_chat_de_enlaces_esta_en_celery_worker(self):
        """notificar_cambios_enlaces_task corre ahi y en ningun otro lado."""
        self.assertIn('ENLACES_TELEGRAM_CHAT_ID', self._entorno_del_servicio('celery_worker'))

    def test_ningun_secreto_nuevo_esta_escrito_literal_en_el_compose(self):
        """Los valores van por ${VAR}, nunca embebidos: el compose se versiona."""
        from django.conf import settings

        contenido = (settings.BASE_DIR / 'deploy' / 'docker-compose.yml').read_text(encoding='utf-8')
        for variable in ('TELEGRAM_BOT_TOKEN', 'ANTHROPIC_API_KEY'):
            for linea in contenido.splitlines():
                if linea.strip().startswith(f'{variable}:'):
                    self.assertIn('${', linea, f'{variable} tiene un valor literal en el compose')


@override_settings(TELEGRAM_BOT_TOKEN='TOKEN-SECRETO-DE-PRUEBA')
class TrozosTelegramTests(TestCase):
    """Telegram rechaza mensajes de mas de 4096 caracteres.

    Encontrado en produccion el 17-sep-2026, no en teoria: la primera corrida real tras
    configurar el chat junto 29 caidas y 128 recuperaciones en un mensaje, Telegram lo
    rechazo con "message is too long", y como notificar_cambios_enlaces marca los eventos
    como avisados aunque el envio falle, esos 157 avisos se perdieron.
    """

    def test_un_mensaje_corto_va_en_un_solo_envio(self):
        from apps.monitoreo.services import _trozos_telegram

        self.assertEqual(_trozos_telegram('hola'), ['hola'])

    def test_un_mensaje_largo_se_parte_y_ningun_trozo_excede_el_limite(self):
        from apps.monitoreo.services import _LIMITE_TELEGRAM, _trozos_telegram

        texto = '\n'.join(f'  GP{i:03d}      192.168.1.{i}  desde 14:20  circuito: proveedor-sitio-{i}'
                          for i in range(200))
        trozos = _trozos_telegram(texto)
        self.assertGreater(len(trozos), 1)
        for trozo in trozos:
            self.assertLessEqual(len(trozo), _LIMITE_TELEGRAM)

    def test_no_se_pierde_ni_se_duplica_una_sola_linea(self):
        """Lo que importa de trocear un listado: que esten todas las farmacias."""
        from apps.monitoreo.services import _trozos_telegram

        lineas = [f'linea numero {i} con texto de relleno para llegar al limite' for i in range(300)]
        trozos = _trozos_telegram('\n'.join(lineas))
        recompuesto = '\n'.join(trozos).split('\n')
        self.assertEqual(recompuesto, lineas)

    def test_una_linea_gigante_sola_no_cuelga_ni_se_descarta(self):
        from apps.monitoreo.services import _LIMITE_TELEGRAM, _trozos_telegram

        trozos = _trozos_telegram('Z' * (_LIMITE_TELEGRAM * 3 + 17))
        self.assertEqual(''.join(trozos), 'Z' * (_LIMITE_TELEGRAM * 3 + 17))
        for trozo in trozos:
            self.assertLessEqual(len(trozo), _LIMITE_TELEGRAM)

    def test_un_resumen_largo_se_manda_en_varios_envios(self):
        from apps.monitoreo.services import _enviar_telegram

        texto = '\n'.join(f'  GP{i:03d}  192.168.1.{i}  circuito: proveedor-{i}' for i in range(300))
        with patch('apps.monitoreo.services.urllib.request.urlopen') as urlopen:
            enviado = _enviar_telegram('-100999', texto)

        self.assertTrue(enviado)
        self.assertGreater(urlopen.call_count, 1, 'un solo POST lo rechazaria Telegram entero')

    def test_si_falla_un_trozo_se_reporta_como_no_enviado(self):
        """El diagnostico de IA no debe darse por entregado con la mitad del mensaje."""
        import urllib.error

        from apps.monitoreo.services import _enviar_telegram

        texto = '\n'.join(f'linea {i} de relleno para superar el limite del mensaje' for i in range(300))
        with patch('apps.monitoreo.services.urllib.request.urlopen',
                   side_effect=[None, urllib.error.URLError('caido')]):
            self.assertFalse(_enviar_telegram('-100999', texto))


@override_settings(TELEGRAM_BOT_TOKEN='TOKEN-SECRETO-DE-PRUEBA',
                   TELEGRAM_CHAT_IDS_AUTORIZADOS=['8499615591'])
class BotConsultasTests(TestCase):
    """Consultas de solo lectura por Telegram.

    Lo que mas importa verificar acá no es el formato sino dos cosas: que el bot no le
    conteste a un desconocido (es publico y lo que responde es el mapa de la red), y que
    /enlaces no mezcle las caidas reales con los sitios que nunca respondieron — el
    numero mezclado decia 162 cuando lo accionable eran 13.
    """

    def setUp(self):
        from apps.monitoreo.enlaces import registrar_sondeo
        from apps.monitoreo.models import EstadoEnlaceFarmacia

        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX920')
        self.viva = Farmacia.objects.create(
            codigo='TSTB01', grupo=grupo, unidad_negocio=self.sg, ip_router='10.5.0.1',
            circuito_proveedor='telconet-sitio-uno', ancho_contratado_mbps=10)
        self.caida = Farmacia.objects.create(
            codigo='TSTB02', grupo=grupo, unidad_negocio=self.sg, ip_router='10.5.0.2',
            circuito_proveedor='telconet-sitio-dos')
        self.nunca = Farmacia.objects.create(
            codigo='TSTB03', grupo=grupo, unidad_negocio=self.sg, ip_router='10.5.0.3',
            circuito_proveedor='puntonet-sitio-tres')

        registrar_sondeo(self.viva, True, 12.0)
        registrar_sondeo(self.caida, True, 15.0)
        for _ in range(EstadoEnlaceFarmacia.UMBRAL_FALLAS_CONSECUTIVAS):
            registrar_sondeo(self.caida, False, None)
            registrar_sondeo(self.nunca, False, None)

    # --- autorizacion ---

    def test_un_chat_desconocido_no_recibe_ninguna_respuesta(self):
        """Ni siquiera "no autorizado": confirmar que el bot responde ya es informacion."""
        from apps.monitoreo.telegram_bot import procesar_actualizacion

        update = {'message': {'chat': {'id': 999999}, 'text': '/enlaces'}}
        with patch('apps.monitoreo.services.urllib.request.urlopen') as urlopen:
            respondido = procesar_actualizacion(update)
        self.assertFalse(respondido)
        urlopen.assert_not_called()

    def test_el_chat_autorizado_si_recibe_respuesta(self):
        from apps.monitoreo.telegram_bot import procesar_actualizacion

        update = {'message': {'chat': {'id': 8499615591}, 'text': '/enlaces'}}
        with patch('apps.monitoreo.services.urllib.request.urlopen') as urlopen:
            self.assertTrue(procesar_actualizacion(update))
        urlopen.assert_called()

    @override_settings(TELEGRAM_CHAT_IDS_AUTORIZADOS=[])
    def test_sin_lista_blanca_no_responde_a_nadie(self):
        from apps.monitoreo.telegram_bot import chat_autorizado

        self.assertFalse(chat_autorizado(8499615591))

    # --- /enlaces, que es lo que se pidio ---

    def test_enlaces_cuenta_solo_las_caidas_reales(self):
        from apps.monitoreo.telegram_bot import responder_a

        texto = responder_a('/enlaces')
        self.assertIn('1 enlace(s) caído(s)', texto, texto)
        self.assertIn('TSTB02', texto)
        self.assertNotIn('TSTB03', texto, 'la que nunca respondio no es una caida')
        self.assertIn('nunca respondieron', texto, 'pero si se informa aparte')

    def test_enlaces_dice_hace_cuanto_esta_caido(self):
        from apps.monitoreo.telegram_bot import responder_a
        from apps.monitoreo.models import EventoEnlaceFarmacia

        evento = EventoEnlaceFarmacia.objects.filter(farmacia=self.caida, fin__isnull=True).first()
        EventoEnlaceFarmacia.objects.filter(pk=evento.pk).update(
            inicio=timezone.now() - timedelta(hours=3, minutes=12))

        texto = responder_a('/enlaces')
        self.assertIn('hace 3 h 12 min', texto, texto)

    def test_el_tiempo_sale_del_evento_no_del_ultimo_cambio(self):
        """El evento marca el PRIMER fallo; ultimo_cambio_estado, el sondeo que confirmo
        la caida. Usar el segundo la mostraria mas corta de lo que fue."""
        from apps.monitoreo.models import EstadoEnlaceFarmacia, EventoEnlaceFarmacia
        from apps.monitoreo.telegram_bot import responder_a

        EventoEnlaceFarmacia.objects.filter(farmacia=self.caida, fin__isnull=True).update(
            inicio=timezone.now() - timedelta(hours=5))
        EstadoEnlaceFarmacia.objects.filter(farmacia=self.caida).update(
            ultimo_cambio_estado=timezone.now() - timedelta(minutes=10))

        self.assertIn('hace 5 h', responder_a('/enlaces'))

    def test_enlaces_agrupa_por_proveedor(self):
        from apps.monitoreo.telegram_bot import responder_a

        self.assertIn('telconet', responder_a('/enlaces'))

    def test_sin_caidas_lo_dice_y_no_inventa_una_lista(self):
        from apps.monitoreo.enlaces import registrar_sondeo
        from apps.monitoreo.telegram_bot import responder_a

        registrar_sondeo(self.caida, True, 20.0)
        texto = responder_a('/enlaces')
        self.assertIn('Ningún enlace caído', texto)

    # --- los otros comandos ---

    def test_estado_da_el_resumen_general(self):
        from apps.monitoreo.telegram_bot import responder_a

        texto = responder_a('/estado')
        for esperado in ('Estaciones:', 'Alertas abiertas:', 'Enlaces caídos:'):
            self.assertIn(esperado, texto)

    def test_alertas_lista_las_abiertas(self):
        from apps.monitoreo.models import Alerta, Metrica, ReglaAlerta
        from apps.monitoreo.telegram_bot import responder_a

        usuario = User.objects.create_user(username='u_bot', password='x')
        grupo = Grupo.objects.create(codigo='TRX921')
        farmacia = Farmacia.objects.create(codigo='TSTB09', grupo=grupo, unidad_negocio=self.sg)
        estacion = Estacion.objects.create(
            codigo='TSTB09-A', farmacia=farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA)
        regla = ReglaAlerta.objects.create(
            nombre='CPU alta', metrica=Metrica.CPU_CARGA_PCT, umbral=90, creado_por=usuario)
        Alerta.objects.create(regla=regla, estacion=estacion, valor_disparador=95)

        texto = responder_a('/alertas')
        self.assertIn('TSTB09-A', texto)
        self.assertIn('CPU alta', texto)

    def test_farmacia_muestra_enlace_y_circuito(self):
        from apps.monitoreo.telegram_bot import responder_a

        texto = responder_a('/farmacia TSTB02')
        self.assertIn('TSTB02', texto)
        self.assertIn('telconet-sitio-dos', texto)
        self.assertIn('caído', texto)

    def test_farmacia_acepta_el_codigo_en_minuscula(self):
        from apps.monitoreo.telegram_bot import responder_a

        self.assertIn('TSTB01', responder_a('/farmacia tstb01'))

    def test_farmacia_inexistente_sugiere_parecidas(self):
        from apps.monitoreo.telegram_bot import responder_a

        texto = responder_a('/farmacia TSTB99')
        self.assertIn('No encontré', texto)
        self.assertIn('TSTB01', texto, 'sugiere las del mismo prefijo')

    def test_farmacia_sin_codigo_pide_el_codigo(self):
        from apps.monitoreo.telegram_bot import responder_a

        self.assertIn('Falta el código', responder_a('/farmacia'))

    # --- comportamiento del bot ---

    def test_el_comando_funciona_con_el_sufijo_del_bot(self):
        """En un grupo, Telegram entrega "/enlaces@saidsoftbot"."""
        from apps.monitoreo.telegram_bot import responder_a

        self.assertIsNotNone(responder_a('/enlaces@saidsoftbot'))

    def test_un_comando_desconocido_ofrece_la_ayuda(self):
        from apps.monitoreo.telegram_bot import responder_a

        texto = responder_a('/reiniciar_todo')
        self.assertIn('No conozco', texto)
        self.assertIn('/enlaces', texto)

    def test_texto_suelto_no_recibe_respuesta(self):
        """El bot no conversa: responder a cualquier texto lo volveria ruidoso en grupo."""
        from apps.monitoreo.telegram_bot import responder_a

        self.assertIsNone(responder_a('hola, todo bien?'))

    def test_un_update_sin_texto_no_rompe_el_bucle(self):
        """Una foto o un sticker no pueden dejar el bot mudo hasta que alguien mire."""
        from apps.monitoreo.telegram_bot import procesar_actualizacion

        self.assertFalse(procesar_actualizacion({'message': {'chat': {'id': 8499615591}}}))
        self.assertFalse(procesar_actualizacion({}))

    def test_ningun_comando_modifica_datos(self):
        """Solo lectura: el canal de entrada de un bot publico no acciona sobre la flota."""
        from apps.monitoreo.models import Alerta, EstadoEnlaceFarmacia
        from apps.monitoreo.telegram_bot import responder_a

        antes = (Alerta.objects.count(), EstadoEnlaceFarmacia.objects.count(),
                 list(EstadoEnlaceFarmacia.objects.values_list('alcanzable', flat=True).order_by('pk')))
        for comando in ('/enlaces', '/estado', '/alertas', '/farmacia TSTB02', '/ayuda'):
            responder_a(comando)
        despues = (Alerta.objects.count(), EstadoEnlaceFarmacia.objects.count(),
                   list(EstadoEnlaceFarmacia.objects.values_list('alcanzable', flat=True).order_by('pk')))
        self.assertEqual(antes, despues)

    def test_ninguna_respuesta_incluye_el_token(self):
        from apps.monitoreo.telegram_bot import responder_a

        for comando in ('/enlaces', '/estado', '/alertas', '/farmacia TSTB02', '/ayuda'):
            self.assertNotIn('TOKEN-SECRETO-DE-PRUEBA', responder_a(comando) or '')


class ComposeBotTelegramTests(ComposeMeshCentralTests):
    """El servicio del bot tiene que recibir el token y la lista blanca."""

    def test_el_servicio_del_bot_tiene_lo_que_necesita(self):
        entorno = self._entorno_del_servicio('telegram_bot')
        for variable in ('TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_IDS_AUTORIZADOS', 'DATABASE_URL'):
            self.assertIn(variable, entorno, variable)

    def test_el_bot_no_publica_ningun_puerto(self):
        """Long polling: sale a buscar, nadie tiene que alcanzarlo desde afuera."""
        from django.conf import settings

        contenido = (settings.BASE_DIR / 'deploy' / 'docker-compose.yml').read_text(encoding='utf-8')
        bloque = contenido.split('\n  telegram_bot:\n', 1)[1].split('\n  redis:\n', 1)[0]
        self.assertNotIn('ports:', bloque)


class ServicioPosNuncaRespondioTests(TestCase):
    """Un servicio que NUNCA respondio no es una caida: es configuracion vieja.

    Encontrado el 18-sep-2026. Odoo (192.168.112.125:8069) figuraba en el .exe.Config del
    POS de las 9 estaciones con agente y no habia respondido ni una vez desde que existe
    el monitor — el servicio esta de baja. Cada estacion abria su alerta "Servicio del POS
    sin responder" por algo que nadie iba a arreglar, y el usuario la recibia sin que
    hubiera pasado nada en su POS.
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX940')
        farmacia = Farmacia.objects.create(codigo='TSTN10', grupo=grupo, unidad_negocio=self.sg)
        self.estacion = Estacion.objects.create(
            codigo='TSTN10-A', farmacia=farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA)
        usuario = User.objects.create_user(username='u_pos_nunca', password='x')
        self.regla = ReglaAlerta.objects.create(
            nombre='Servicio del POS sin responder (no critico)',
            metrica=Metrica.SERVICIO_POS_CAIDO, umbral=0,
            severidad=ReglaAlerta.Severidad.WARNING, creado_por=usuario)

    def _registrar(self, servicio, disponible, *, respondio_antes):
        """Deja un EstadoServicioPos como lo dejaria el agente."""
        from apps.monitoreo.models import EstadoServicioPos

        ahora = timezone.now()
        estado, _ = EstadoServicioPos.objects.update_or_create(
            estacion=self.estacion, servicio=servicio,
            defaults={
                'disponible': disponible,
                'mensaje': 'PostgreSQL 160014' if disponible else '<urlopen error timed out>',
                'endpoint': 'http://192.168.112.125:8069',
                'critico': False,
                'ultima_verificacion': ahora,
                # Lo unico que distingue los dos casos: si alguna vez contesto.
                'ultima_respuesta': ahora if disponible else (
                    ahora - timedelta(hours=4) if respondio_antes else None),
            },
        )
        return estado

    def test_un_servicio_que_nunca_respondio_no_abre_alerta(self):
        from apps.monitoreo.models import Alerta, ServicioPos
        from apps.monitoreo.services import evaluar_regla_servicio_pos

        estado = self._registrar(ServicioPos.ODOO, False, respondio_antes=False)
        evaluar_regla_servicio_pos(self.estacion, estado)

        self.assertFalse(
            Alerta.objects.filter(regla=self.regla, estado=Alerta.Estado.ABIERTA).exists(),
            'un servicio dado de baja no es un incidente que atender',
        )

    def test_un_servicio_que_si_respondia_y_se_cayo_SI_abre_alerta(self):
        """El contraste: esto sigue siendo una caida real y tiene que avisar."""
        from apps.monitoreo.models import Alerta, ServicioPos
        from apps.monitoreo.services import evaluar_regla_servicio_pos

        estado = self._registrar(ServicioPos.PG_CENTRAL, False, respondio_antes=True)
        evaluar_regla_servicio_pos(self.estacion, estado)

        self.assertTrue(
            Alerta.objects.filter(regla=self.regla, estado=Alerta.Estado.ABIERTA).exists())

    def test_uno_de_baja_no_tapa_la_caida_real_de_otro(self):
        """Los tres no criticos comparten regla: el que nunca respondio se ignora, pero
        el que si se cayo tiene que abrir igual."""
        from apps.monitoreo.models import Alerta, ServicioPos
        from apps.monitoreo.services import evaluar_regla_servicio_pos

        self._registrar(ServicioPos.ODOO, False, respondio_antes=False)
        estado = self._registrar(ServicioPos.PG_CENTRAL, False, respondio_antes=True)
        evaluar_regla_servicio_pos(self.estacion, estado)

        self.assertTrue(
            Alerta.objects.filter(regla=self.regla, estado=Alerta.Estado.ABIERTA).exists())

    def test_la_alerta_se_resuelve_si_lo_unico_caido_nunca_respondio(self):
        from apps.monitoreo.models import Alerta, ServicioPos
        from apps.monitoreo.services import evaluar_regla_servicio_pos

        Alerta.objects.create(regla=self.regla, estacion=self.estacion, valor_disparador=0)
        self._registrar(ServicioPos.ODOO, False, respondio_antes=False)
        estado = self._registrar(ServicioPos.PG_CENTRAL, True, respondio_antes=True)
        evaluar_regla_servicio_pos(self.estacion, estado)

        self.assertFalse(
            Alerta.objects.filter(regla=self.regla, estado=Alerta.Estado.ABIERTA).exists(),
            'lo unico sin responder es un servicio de baja: la alerta no tiene por que seguir',
        )

    def test_la_propiedad_distingue_los_dos_casos(self):
        from apps.monitoreo.models import ServicioPos

        nunca = self._registrar(ServicioPos.ODOO, False, respondio_antes=False)
        caido = self._registrar(ServicioPos.PG_CENTRAL, False, respondio_antes=True)
        self.assertTrue(nunca.nunca_respondio)
        self.assertFalse(caido.nunca_respondio)
        self.assertIsNone(nunca.horas_sin_responder)
        self.assertIsNotNone(caido.horas_sin_responder)


@override_settings(TELEGRAM_BOT_TOKEN='TOKEN-SECRETO-DE-PRUEBA',
                   TELEGRAM_CHAT_IDS_AUTORIZADOS=['8499615591'])
class TecladoInlineTests(TestCase):
    """Botones en vez de escribir comandos."""

    def setUp(self):
        from apps.monitoreo.enlaces import registrar_sondeo
        from apps.monitoreo.models import EstadoEnlaceFarmacia

        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX950')
        self.caida = Farmacia.objects.create(
            codigo='TSTK10', grupo=grupo, unidad_negocio=self.sg, ip_router='10.7.0.1',
            circuito_proveedor='telconet-uno')
        registrar_sondeo(self.caida, True, 10.0)
        for _ in range(EstadoEnlaceFarmacia.UMBRAL_FALLAS_CONSECUTIVAS):
            registrar_sondeo(self.caida, False, None)

    def _cuerpos(self, urlopen):
        return [json.loads(l.args[0].data.decode('utf-8')) for l in urlopen.call_args_list]

    def _metodos(self, urlopen):
        return [l.args[0].full_url.rsplit('/', 1)[-1] for l in urlopen.call_args_list]

    # --- el teclado viaja con la respuesta ---

    def test_una_consulta_escrita_responde_con_botones(self):
        from apps.monitoreo.telegram_bot import procesar_actualizacion

        update = {'message': {'chat': {'id': 8499615591}, 'text': '/estado'}}
        with patch('apps.monitoreo.services.urllib.request.urlopen') as urlopen:
            procesar_actualizacion(update)

        cuerpo = self._cuerpos(urlopen)[-1]
        filas = cuerpo['reply_markup']['inline_keyboard']
        etiquetas = [b['text'] for fila in filas for b in fila]
        self.assertTrue(any('Enlaces' in e for e in etiquetas), etiquetas)
        self.assertTrue(any('Alertas' in e for e in etiquetas), etiquetas)

    def test_el_teclado_va_solo_en_el_ultimo_trozo_de_un_mensaje_largo(self):
        """Repetirlo en cada trozo dejaria tres filas de botones y la de arriba operaria
        sobre un mensaje viejo."""
        from apps.monitoreo.services import _enviar_telegram

        texto = '\n'.join(f'linea {i} de relleno para pasar el limite' for i in range(300))
        teclado = [[{'text': 'x', 'callback_data': 'cmd:estado'}]]
        with patch('apps.monitoreo.services.urllib.request.urlopen') as urlopen:
            _enviar_telegram('8499615591', texto, teclado=teclado)

        cuerpos = self._cuerpos(urlopen)
        self.assertGreater(len(cuerpos), 1)
        self.assertNotIn('reply_markup', cuerpos[0])
        self.assertIn('reply_markup', cuerpos[-1])

    # --- los botones ---

    def test_un_boton_devuelve_lo_mismo_que_el_comando_escrito(self):
        from apps.monitoreo.telegram_bot import responder_a, responder_a_callback

        texto, teclado = responder_a_callback('cmd:enlaces')
        self.assertEqual(texto, responder_a('/enlaces'))
        self.assertTrue(teclado)

    def test_el_submenu_ofrece_las_farmacias_con_problemas(self):
        from apps.monitoreo.telegram_bot import responder_a_callback

        _texto, teclado = responder_a_callback('menu:farmacias')
        etiquetas = [b['text'] for fila in teclado for b in fila]
        self.assertIn('TSTK10', etiquetas, etiquetas)
        self.assertTrue(any('Volver' in e for e in etiquetas))

    def test_el_boton_de_una_farmacia_trae_su_detalle(self):
        from apps.monitoreo.telegram_bot import responder_a_callback

        texto, _teclado = responder_a_callback('farm:TSTK10')
        self.assertIn('TSTK10', texto)
        self.assertIn('telconet-uno', texto)

    def test_un_callback_desconocido_no_hace_nada(self):
        from apps.monitoreo.telegram_bot import responder_a_callback

        self.assertIsNone(responder_a_callback('cmd:borrar_todo'))
        self.assertIsNone(responder_a_callback(''))
        self.assertIsNone(responder_a_callback(None))

    def test_callback_data_nunca_supera_el_tope_de_telegram(self):
        """64 bytes es un limite duro: pasarlo hace que Telegram rechace el teclado."""
        from apps.monitoreo.telegram_bot import _TECLADO_PRINCIPAL, _teclado_farmacias

        for teclado in (_TECLADO_PRINCIPAL, _teclado_farmacias()):
            for fila in teclado:
                for boton in fila:
                    self.assertLessEqual(len(boton['callback_data'].encode('utf-8')), 64, boton)

    # --- el reloj del boton ---

    def test_siempre_se_confirma_el_boton_aunque_no_se_responda(self):
        """Sin answerCallbackQuery el boton le queda al operador girando y parece colgado."""
        from apps.monitoreo.telegram_bot import procesar_actualizacion

        update = {'callback_query': {'id': 'abc123', 'data': 'cmd:inexistente',
                                     'message': {'chat': {'id': 8499615591}}}}
        with patch('apps.monitoreo.services.urllib.request.urlopen') as urlopen:
            procesar_actualizacion(update)
        self.assertIn('answerCallbackQuery', self._metodos(urlopen))

    def test_un_boton_pulsado_desde_un_chat_no_autorizado_no_responde(self):
        from apps.monitoreo.telegram_bot import procesar_actualizacion

        update = {'callback_query': {'id': 'abc', 'data': 'cmd:enlaces',
                                     'message': {'chat': {'id': 111222}}}}
        with patch('apps.monitoreo.services.urllib.request.urlopen') as urlopen:
            self.assertFalse(procesar_actualizacion(update))
        self.assertNotIn('sendMessage', self._metodos(urlopen))

    def test_el_boton_responde_de_punta_a_punta(self):
        from apps.monitoreo.telegram_bot import procesar_actualizacion

        update = {'callback_query': {'id': 'abc', 'data': 'cmd:enlaces',
                                     'message': {'chat': {'id': 8499615591}}}}
        with patch('apps.monitoreo.services.urllib.request.urlopen') as urlopen:
            self.assertTrue(procesar_actualizacion(update))
        metodos = self._metodos(urlopen)
        self.assertEqual(metodos[0], 'answerCallbackQuery', 'primero se saca el reloj')
        self.assertIn('sendMessage', metodos)

    def test_el_worker_pide_los_callback_query_a_telegram(self):
        """Telegram NO entrega los botones si no estan en allowed_updates."""
        from django.conf import settings

        ruta = (settings.BASE_DIR / 'apps' / 'monitoreo' / 'management' / 'commands'
                / 'run_telegram_bot.py')
        self.assertIn("'callback_query'", ruta.read_text(encoding='utf-8'))


@override_settings(TELEGRAM_BOT_TOKEN='TOKEN-SECRETO-DE-PRUEBA',
                   TELEGRAM_CHAT_IDS_AUTORIZADOS=['8499615591'])
class ComandosCriticasMantenimientoTopErroresTests(TestCase):
    """/criticas, /mantenimiento y /toperrores."""

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.grupo = Grupo.objects.create(codigo='TRX960')
        self.farmacia = Farmacia.objects.create(
            codigo='TSTC01', grupo=self.grupo, unidad_negocio=self.sg, ip_router='10.9.0.1')
        self.estacion = Estacion.objects.create(
            codigo='TSTC01-A', farmacia=self.farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA)
        self.otra = Estacion.objects.create(
            codigo='TSTC01-B', farmacia=self.farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA)
        self.usuario = User.objects.create_user(username='u_cmd_nuevos', password='x')

    def _alerta(self, severidad, nombre):
        regla = ReglaAlerta.objects.create(
            nombre=nombre, metrica=Metrica.CPU_CARGA_PCT, umbral=90,
            severidad=severidad, creado_por=self.usuario)
        return Alerta.objects.create(regla=regla, estacion=self.estacion, valor_disparador=95)

    # --- /criticas ---

    def test_criticas_deja_afuera_las_advertencias(self):
        from apps.monitoreo.telegram_bot import responder_a

        self._alerta(ReglaAlerta.Severidad.CRITICAL, 'Base local caida')
        self._alerta(ReglaAlerta.Severidad.WARNING, 'CPU alta')

        texto = responder_a('/criticas')
        self.assertIn('Base local caida', texto)
        self.assertNotIn('CPU alta', texto)
        self.assertIn('CRÍTICA', texto)

    def test_alertas_sigue_mostrando_las_dos(self):
        """La factorizacion no puede cambiar lo que ya devolvia /alertas."""
        from apps.monitoreo.telegram_bot import responder_a

        self._alerta(ReglaAlerta.Severidad.CRITICAL, 'Base local caida')
        self._alerta(ReglaAlerta.Severidad.WARNING, 'CPU alta')

        texto = responder_a('/alertas')
        self.assertIn('Base local caida', texto)
        self.assertIn('CPU alta', texto)

    def test_sin_criticas_lo_dice_sin_inventar_una_lista(self):
        from apps.monitoreo.telegram_bot import responder_a

        self._alerta(ReglaAlerta.Severidad.WARNING, 'CPU alta')
        self.assertIn('No hay alertas críticas', responder_a('/criticas'))

    # --- /mantenimiento ---

    def _ventana(self, motivo, *, desde_horas, hasta_horas, activo=True, destino='cadena'):
        from apps.monitoreo.models import VentanaMantenimiento

        ahora = timezone.now()
        return VentanaMantenimiento.objects.create(
            unidad_negocio=self.sg, destino_tipo=destino,
            desde=ahora + timedelta(hours=desde_horas),
            hasta=ahora + timedelta(hours=hasta_horas),
            motivo=motivo, activo=activo, creado_por=self.usuario,
        )

    def test_mantenimiento_solo_muestra_la_ventana_en_curso(self):
        """Punto 3 de la verificacion: una vencida y una activa."""
        from apps.monitoreo.telegram_bot import responder_a

        self._ventana('despliegue de ayer', desde_horas=-48, hasta_horas=-24)
        self._ventana('reinicio programado de hoy', desde_horas=-1, hasta_horas=2)

        texto = responder_a('/mantenimiento')
        self.assertIn('reinicio programado de hoy', texto)
        self.assertNotIn('despliegue de ayer', texto)

    def test_una_ventana_futura_todavia_no_aparece(self):
        from apps.monitoreo.telegram_bot import responder_a

        self._ventana('mantenimiento de la semana que viene', desde_horas=48, hasta_horas=72)
        self.assertIn('Sin ventanas', responder_a('/mantenimiento'))

    def test_una_ventana_desactivada_no_aparece_aunque_este_en_rango(self):
        """activo=False es el mismo criterio que usa ventana_mantenimiento_activa para
        NO silenciar: mostrarla diria que hay alertas calladas cuando no las hay."""
        from apps.monitoreo.telegram_bot import responder_a

        self._ventana('desactivada', desde_horas=-1, hasta_horas=2, activo=False)
        self.assertIn('Sin ventanas', responder_a('/mantenimiento'))

    def test_mantenimiento_cuenta_las_estaciones_del_destino(self):
        from apps.monitoreo.telegram_bot import responder_a

        self._ventana('despliegue de POS', desde_horas=-1, hasta_horas=3)
        texto = responder_a('/mantenimiento')
        # Con pocas estaciones se listan los codigos en vez del numero.
        self.assertIn('TSTC01-A', texto)
        self.assertIn('TSTC01-B', texto)

    def test_sin_ventanas_lo_dice_corto(self):
        from apps.monitoreo.telegram_bot import responder_a

        self.assertEqual(responder_a('/mantenimiento'), 'Sin ventanas de mantenimiento activas.')

    # --- /toperrores ---

    def _error(self, estacion, mensaje, cantidad, categoria):
        from apps.monitoreo.models import PosErrorDetectado

        return PosErrorDetectado.objects.create(
            estacion=estacion, mensaje=mensaje, cantidad_total=cantidad, categoria=categoria)

    def test_toperrores_no_cuenta_los_de_negocio(self):
        """Punto 2 de la verificacion. "VENTA SIN LOTE" es el POS validando bien: con
        3.000 repeticiones le ganaria a cualquier bug real y el ranking seria inutil."""
        from apps.monitoreo.models import PosErrorDetectado
        from apps.monitoreo.telegram_bot import responder_a

        self._error(self.estacion, 'VENTA SIN LOTE en el detalle', 3000,
                    PosErrorDetectado.Categoria.NEGOCIO)
        self._error(self.estacion, 'Timeout conectando a la base', 12,
                    PosErrorDetectado.Categoria.SISTEMA)

        texto = responder_a('/toperrores')
        # La nota al pie nombra "VENTA SIN LOTE" como ejemplo de lo excluido, asi que se
        # mira el ranking sin esa linea: lo que importa es que no COMPITA, no que la
        # palabra no aparezca.
        ranking = texto.split('No se cuentan')[0]
        self.assertIn('Timeout conectando a la base', ranking)
        self.assertNotIn('VENTA SIN LOTE', ranking)
        self.assertNotIn('3000', ranking, 'sus 3.000 repeticiones no pueden encabezar el ranking')

    def test_toperrores_ordena_por_cantidad_total(self):
        from apps.monitoreo.models import PosErrorDetectado
        from apps.monitoreo.telegram_bot import responder_a

        self._error(self.estacion, 'error poco frecuente', 5, PosErrorDetectado.Categoria.SISTEMA)
        self._error(self.estacion, 'error muy frecuente', 500, PosErrorDetectado.Categoria.SISTEMA)

        texto = responder_a('/toperrores')
        self.assertLess(texto.index('error muy frecuente'), texto.index('error poco frecuente'))

    def test_toperrores_suma_la_flota_y_cuenta_estaciones_distintas(self):
        """El punto del comando: el mismo bug repetido en varias farmacias."""
        from apps.monitoreo.models import PosErrorDetectado
        from apps.monitoreo.telegram_bot import responder_a

        self._error(self.estacion, 'relacion faltante en la base', 10,
                    PosErrorDetectado.Categoria.SISTEMA)
        self._error(self.otra, 'relacion faltante en la base', 15,
                    PosErrorDetectado.Categoria.SISTEMA)

        texto = responder_a('/toperrores')
        self.assertIn('x25', texto, texto)
        self.assertIn('2 estación(es)', texto)

    def test_sin_errores_de_sistema_lo_dice(self):
        from apps.monitoreo.models import PosErrorDetectado
        from apps.monitoreo.telegram_bot import responder_a

        self._error(self.estacion, 'VENTA SIN LOTE', 99, PosErrorDetectado.Categoria.NEGOCIO)
        self.assertIn('Sin errores de sistema', responder_a('/toperrores'))

    # --- solo lectura ---

    def test_los_comandos_nuevos_no_escriben_nada(self):
        from apps.monitoreo.models import Alerta, PosErrorDetectado, VentanaMantenimiento
        from apps.monitoreo.telegram_bot import responder_a

        self._alerta(ReglaAlerta.Severidad.CRITICAL, 'Base local caida')
        self._ventana('algo', desde_horas=-1, hasta_horas=2)
        self._error(self.estacion, 'un error', 3, PosErrorDetectado.Categoria.SISTEMA)

        antes = (Alerta.objects.count(), VentanaMantenimiento.objects.count(),
                 PosErrorDetectado.objects.count())
        for comando in ('/criticas', '/mantenimiento', '/toperrores'):
            responder_a(comando)
        despues = (Alerta.objects.count(), VentanaMantenimiento.objects.count(),
                   PosErrorDetectado.objects.count())
        self.assertEqual(antes, despues)

    # --- teclado ---

    def test_los_comandos_nuevos_estan_detras_del_submenu(self):
        """Siete botones en la pantalla principal se leen peor que cuatro."""
        from apps.monitoreo.telegram_bot import _TECLADO_PRINCIPAL, responder_a_callback

        principales = [b['callback_data'] for fila in _TECLADO_PRINCIPAL for b in fila]
        self.assertNotIn('cmd:criticas', principales)
        self.assertIn('menu:mas', principales)

        _texto, teclado = responder_a_callback('menu:mas')
        datos = [b['callback_data'] for fila in teclado for b in fila]
        for esperado in ('cmd:criticas', 'cmd:mantenimiento', 'cmd:toperrores'):
            self.assertIn(esperado, datos)

    def test_un_boton_del_submenu_devuelve_al_submenu(self):
        """Volver al principal obligaria a entrar a "Mas" otra vez en cada consulta."""
        from apps.monitoreo.telegram_bot import responder_a_callback

        _texto, teclado = responder_a_callback('cmd:toperrores')
        datos = [b['callback_data'] for fila in teclado for b in fila]
        self.assertIn('cmd:criticas', datos)


@override_settings(TELEGRAM_BOT_TOKEN='TOKEN-SECRETO-DE-PRUEBA')
class ResumenDiarioTelegramTests(TestCase):
    """El resumen diario sale por CanalNotificacion, no por la lista de autorizados."""

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        self.usuario = User.objects.create_user(username='u_resumen', password='x')

    def _canal(self, destino, unidad=None):
        return CanalNotificacion.objects.create(
            tipo=CanalNotificacion.Tipo.TELEGRAM, destino=destino,
            unidad_negocio=unidad, creado_por=self.usuario)

    def _textos(self, urlopen):
        return [json.loads(l.args[0].data.decode('utf-8'))['text'] for l in urlopen.call_args_list]

    def test_manda_el_mismo_contenido_que_estado(self):
        from apps.monitoreo.tasks import resumen_diario_telegram_task
        from apps.monitoreo.telegram_bot import _comando_estado

        self._canal('-100555')
        with patch('apps.monitoreo.services.urllib.request.urlopen') as urlopen:
            resultado = resumen_diario_telegram_task()

        enviado = self._textos(urlopen)[0]
        self.assertIn('Estaciones:', enviado)
        self.assertIn('Enlaces caídos:', enviado)
        # El cuerpo es el de /estado, con un saludo adelante.
        for linea in _comando_estado().splitlines():
            if linea.strip():
                self.assertIn(linea.split(':')[0], enviado)
        self.assertIn('1 de 1', resultado)

    def test_no_usa_la_lista_de_chats_autorizados(self):
        """Esa lista es control de acceso a las consultas entrantes, no un destino."""
        from apps.monitoreo.tasks import resumen_diario_telegram_task

        with override_settings(TELEGRAM_CHAT_IDS_AUTORIZADOS=['8499615591']):
            with patch('apps.monitoreo.services.urllib.request.urlopen') as urlopen:
                resultado = resumen_diario_telegram_task()
        urlopen.assert_not_called()
        self.assertIn('Sin canales', resultado)

    def test_no_le_manda_el_resumen_global_a_un_canal_de_una_unidad(self):
        """El texto habla de la flota entera: mandarselo al canal de MIA le mostraria
        cuantos enlaces de San Gregorio estan caidos."""
        from apps.monitoreo.tasks import resumen_diario_telegram_task

        self._canal('-100777', unidad=self.mia)
        with patch('apps.monitoreo.services.urllib.request.urlopen') as urlopen:
            resumen_diario_telegram_task()
        urlopen.assert_not_called()

    def test_un_canal_inactivo_no_recibe(self):
        from apps.monitoreo.tasks import resumen_diario_telegram_task

        canal = self._canal('-100555')
        canal.activo = False
        canal.save(update_fields=['activo'])
        with patch('apps.monitoreo.services.urllib.request.urlopen') as urlopen:
            resumen_diario_telegram_task()
        urlopen.assert_not_called()

    def test_un_canal_de_teams_no_recibe_el_resumen_de_telegram(self):
        from apps.monitoreo.tasks import resumen_diario_telegram_task

        CanalNotificacion.objects.create(
            tipo=CanalNotificacion.Tipo.WEBHOOK_TEAMS, destino='https://outlook.office.com/webhook/x',
            creado_por=self.usuario)
        with patch('apps.monitoreo.services.urllib.request.urlopen') as urlopen:
            resultado = resumen_diario_telegram_task()
        urlopen.assert_not_called()
        self.assertIn('Sin canales', resultado)

    def test_la_tarea_esta_programada_a_una_hora_fija(self):
        from django.conf import settings

        entrada = settings.CELERY_BEAT_SCHEDULE['resumen-diario-telegram']
        self.assertEqual(entrada['task'], 'apps.monitoreo.tasks.resumen_diario_telegram_task')
        # crontab y no un intervalo: tiene que caer a una hora del dia.
        self.assertEqual(set(entrada['schedule'].hour), {8})
        self.assertEqual(set(entrada['schedule'].minute), {0})

    def test_el_crontab_queda_en_hora_local(self):
        """Con CELERY_TIMEZONE en UTC, las 8:00 caerian a las 3 de la manana en Ecuador."""
        from django.conf import settings

        self.assertEqual(settings.CELERY_TIMEZONE, 'America/Guayaquil')

    def test_el_resumen_no_escribe_en_la_base(self):
        from apps.monitoreo.models import Alerta
        from apps.monitoreo.tasks import resumen_diario_telegram_task

        self._canal('-100555')
        antes = Alerta.objects.count()
        with patch('apps.monitoreo.services.urllib.request.urlopen'):
            resumen_diario_telegram_task()
        self.assertEqual(Alerta.objects.count(), antes)


class TopErroresVentanaTests(TestCase):
    """El ranking muestra lo que esta pasando, no lo que paso alguna vez.

    PosErrorDetectado es un contador de por vida, asi que sin corte temporal un problema
    resuelto sigue encabezando para siempre. El 18-sep-2026 el segundo puesto lo ocupaba
    un '3D000: no existe la base de datos "TRX004"' de ML027-ADM del 27 de agosto, ya
    arreglado: el ranking mandaba a revisar algo que no existia.
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX970')
        farmacia = Farmacia.objects.create(codigo='TSTV01', grupo=grupo, unidad_negocio=self.sg)
        self.estacion = Estacion.objects.create(
            codigo='TSTV01-A', farmacia=farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA)

    def _error(self, mensaje, cantidad, *, dias_atras):
        """auto_now obliga a pisar ultima_vez con un update despues de crear."""
        from apps.monitoreo.models import PosErrorDetectado

        obj = PosErrorDetectado.objects.create(
            estacion=self.estacion, mensaje=mensaje, cantidad_total=cantidad,
            categoria=PosErrorDetectado.Categoria.SISTEMA)
        PosErrorDetectado.objects.filter(pk=obj.pk).update(
            ultima_vez=timezone.now() - timedelta(days=dias_atras))
        return obj

    def test_un_error_viejo_no_encabeza_el_ranking(self):
        from apps.monitoreo.telegram_bot import responder_a

        self._error('problema arreglado hace tres semanas', 5000, dias_atras=22)
        self._error('problema de esta semana', 3, dias_atras=1)

        texto = responder_a('/toperrores')
        self.assertIn('problema de esta semana', texto)
        self.assertNotIn('arreglado hace tres semanas', texto,
                         'con 5.000 repeticiones viejas igual no entra')

    def test_el_borde_de_la_ventana(self):
        from apps.monitoreo.telegram_bot import _DIAS_TOPERRORES, responder_a

        self._error('justo adentro', 10, dias_atras=_DIAS_TOPERRORES - 1)
        self._error('justo afuera', 10, dias_atras=_DIAS_TOPERRORES + 1)

        texto = responder_a('/toperrores')
        self.assertIn('justo adentro', texto)
        self.assertNotIn('justo afuera', texto)

    def test_dice_hace_cuanto_se_vio_cada_uno(self):
        """Sin esto, dos errores de la ventana se ven igual de urgentes."""
        from apps.monitoreo.telegram_bot import responder_a

        self._error('error de ayer', 4, dias_atras=1)
        self.assertIn('visto hace 1 d', responder_a('/toperrores'))

    def test_aclara_que_el_total_es_historico(self):
        """"x500" no puede leerse como "500 veces esta semana": el modelo no guarda el
        desglose por dia, asi que el numero es el acumulado del mensaje."""
        from apps.monitoreo.telegram_bot import responder_a

        self._error('algo', 500, dias_atras=1)
        self.assertIn('histórico', responder_a('/toperrores'))

    def test_sin_nada_reciente_lo_dice_y_no_muestra_lo_viejo(self):
        from apps.monitoreo.telegram_bot import responder_a

        self._error('solo cosas viejas', 900, dias_atras=60)
        texto = responder_a('/toperrores')
        self.assertIn('Sin errores de sistema', texto)
        self.assertNotIn('solo cosas viejas', texto)


@override_settings(ENLACES_NOTIFICAR_A=['redes@ejemplo.com'])
class ParpadeoDeEnlacesTests(TestCase):
    """Cortes breves que no tienen que generar aviso.

    El usuario reporto el 18-sep-2026 recibir "GP092 estuvo caido 2 min". Dos problemas
    distintos detras del mismo sintoma: la recuperacion se avisaba aunque la caida nunca
    se hubiera avisado (12 de 85 caidas de ese dia), y la duracion estaba mal calculada
    — el evento arrancaba en el tercer fallo, no en el primero, asi que GP092 figuraba
    con 2 minutos cuando el enlace habia estado mal unos 8.
    """

    def setUp(self):
        from apps.monitoreo.models import EstadoEnlaceFarmacia

        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX980')
        self.farmacia = Farmacia.objects.create(
            codigo='TSTP01', grupo=grupo, unidad_negocio=self.sg, ip_router='10.8.0.1',
            circuito_proveedor='telconet-prueba')
        self.UMBRAL = EstadoEnlaceFarmacia.UMBRAL_FALLAS_CONSECUTIVAS
        _fijar_umbral_aviso(10)  # esta clase SI prueba el umbral
        mail.outbox = []

    def _sondear(self, alcanzable, veces=1):
        from apps.monitoreo.enlaces import registrar_sondeo

        for _ in range(veces):
            registrar_sondeo(self.farmacia, alcanzable, 12.0 if alcanzable else None)

    def _caer(self):
        self._sondear(True)
        self._sondear(False, veces=self.UMBRAL)

    def _envejecer(self, minutos):
        """Mueve el inicio de la caida abierta hacia atras."""
        from apps.monitoreo.models import EventoEnlaceFarmacia

        EventoEnlaceFarmacia.objects.filter(farmacia=self.farmacia, fin__isnull=True).update(
            inicio=timezone.now() - timedelta(minutes=minutos))

    # --- el inicio del evento ---

    def test_la_caida_arranca_en_el_primer_fallo_no_en_el_tercero(self):
        """Lo que hacia que GP092 figurara con 2 min en vez de 8."""
        from apps.monitoreo.models import EstadoEnlaceFarmacia, EventoEnlaceFarmacia

        self._sondear(True)
        antes = timezone.now()
        self._sondear(False, veces=self.UMBRAL)

        evento = EventoEnlaceFarmacia.objects.get(farmacia=self.farmacia)
        estado = EstadoEnlaceFarmacia.objects.get(farmacia=self.farmacia)
        self.assertLessEqual(evento.inicio, estado.ultimo_cambio_estado,
                             'el inicio no puede ser posterior al momento en que se confirmo')
        self.assertGreaterEqual(evento.inicio, antes)

    def test_al_responder_se_limpia_la_marca_del_primer_fallo(self):
        """Si no, la proxima racha heredaria el inicio de la anterior."""
        from apps.monitoreo.models import EstadoEnlaceFarmacia

        self._sondear(False)
        self.assertIsNotNone(EstadoEnlaceFarmacia.objects.get(farmacia=self.farmacia).primer_fallo)
        self._sondear(True)
        self.assertIsNone(EstadoEnlaceFarmacia.objects.get(farmacia=self.farmacia).primer_fallo)

    def test_un_fallo_suelto_seguido_de_respuesta_no_deja_racha(self):
        from apps.monitoreo.models import EstadoEnlaceFarmacia, EventoEnlaceFarmacia

        self._sondear(True)
        self._sondear(False)
        self._sondear(True)
        self.assertFalse(EventoEnlaceFarmacia.objects.exists(), 'un fallo no es una caida')
        self.assertEqual(
            EstadoEnlaceFarmacia.objects.get(farmacia=self.farmacia).fallas_consecutivas, 0)

    # --- el umbral de duracion ---

    def test_una_caida_recien_confirmada_todavia_no_se_avisa(self):
        from apps.monitoreo.enlaces import notificar_cambios_enlaces

        self._caer()
        resumen = notificar_cambios_enlaces()
        self.assertEqual(resumen['caidos'], 0, 'hay que esperar el minimo antes de avisar')
        self.assertEqual(len(mail.outbox), 0)

    def test_la_misma_caida_se_avisa_cuando_pasa_el_minimo(self):
        """No se pierde: se espera. En la corrida siguiente ya califica."""
        from apps.monitoreo.enlaces import notificar_cambios_enlaces

        self._caer()
        notificar_cambios_enlaces()
        self._envejecer(15)

        resumen = notificar_cambios_enlaces()
        self.assertEqual(resumen['caidos'], 1)
        self.assertIn('TSTP01', mail.outbox[-1].body)

    # --- la recuperacion huerfana ---

    def test_un_parpadeo_completo_no_genera_ningun_aviso(self):
        """El caso exacto que reporto el usuario: cae y vuelve entre dos corridas."""
        from apps.monitoreo.enlaces import notificar_cambios_enlaces

        self._caer()
        self._sondear(True)  # vuelve enseguida

        resumen = notificar_cambios_enlaces()
        self.assertEqual(resumen['caidos'], 0)
        self.assertEqual(resumen['recuperados'], 0,
                         'no se anuncia que volvio algo que nunca se dijo que se fue')
        self.assertEqual(len(mail.outbox), 0)

    def test_la_recuperacion_si_se_avisa_cuando_la_caida_se_habia_avisado(self):
        from apps.monitoreo.enlaces import notificar_cambios_enlaces

        self._caer()
        self._envejecer(15)
        notificar_cambios_enlaces()
        mail.outbox = []

        self._sondear(True)
        resumen = notificar_cambios_enlaces()
        self.assertEqual(resumen['recuperados'], 1)
        self.assertIn('RECUPERADOS', mail.outbox[-1].body)

    def test_un_corte_largo_sigue_avisando_normal(self):
        """El filtro no puede silenciar lo que si importa."""
        from apps.monitoreo.enlaces import notificar_cambios_enlaces

        self._caer()
        self._envejecer(180)
        resumen = notificar_cambios_enlaces()
        self.assertEqual(resumen['caidos'], 1)

    def test_el_umbral_se_puede_desactivar(self):
        """Con 0 vuelve el comportamiento anterior, para quien prefiera enterarse de todo."""
        from apps.monitoreo.enlaces import notificar_cambios_enlaces

        _fijar_umbral_aviso(0)
        self._caer()
        self.assertEqual(notificar_cambios_enlaces()['caidos'], 1)


class ConfiguracionMonitoreoTests(TestCase):
    """Los umbrales operativos, editables sin desplegar.

    Pedido del usuario el 18-sep-2026: "puede que a futuro le baje pero necesito tener el
    control por interfaz". Un valor que se ajusta con la experiencia no puede exigir un
    despliegue para pasar de 10 a 15.
    """

    def test_se_crea_sola_con_los_valores_por_defecto(self):
        """Nunca hay que acordarse de sembrarla."""
        from apps.monitoreo.models import ConfiguracionMonitoreo

        ConfiguracionMonitoreo.objects.all().delete()
        config = ConfiguracionMonitoreo.obtener()
        self.assertEqual(config.minutos_minimos_aviso_enlace, 10)
        self.assertEqual(config.fallas_consecutivas_enlace, 3)
        self.assertEqual(config.minutos_escalamiento_alerta, 30)
        self.assertEqual(config.dias_ventana_top_errores, 7)

    def test_siempre_hay_una_sola_fila(self):
        """Una segunda fila seria configuracion que nadie lee: el operador cambiaria
        valores sin efecto."""
        from apps.monitoreo.models import ConfiguracionMonitoreo

        ConfiguracionMonitoreo.obtener()
        ConfiguracionMonitoreo.objects.create(minutos_minimos_aviso_enlace=99)
        self.assertEqual(ConfiguracionMonitoreo.objects.count(), 1)
        self.assertEqual(ConfiguracionMonitoreo.obtener().minutos_minimos_aviso_enlace, 99)

    # --- que cada umbral llegue a donde se usa ---

    def test_cambiar_los_minutos_minimos_cambia_a_quien_se_avisa(self):
        from apps.monitoreo.enlaces import notificar_cambios_enlaces, registrar_sondeo
        from apps.monitoreo.models import ConfiguracionMonitoreo, EventoEnlaceFarmacia

        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX990')
        farmacia = Farmacia.objects.create(
            codigo='TSTQ01', grupo=grupo, unidad_negocio=sg, ip_router='10.6.0.1')

        config = ConfiguracionMonitoreo.obtener()
        registrar_sondeo(farmacia, True, 10.0)
        for _ in range(config.fallas_consecutivas_enlace):
            registrar_sondeo(farmacia, False, None)
        EventoEnlaceFarmacia.objects.filter(farmacia=farmacia, fin__isnull=True).update(
            inicio=timezone.now() - timedelta(minutes=12))

        with override_settings(ENLACES_NOTIFICAR_A=['x@y.z']):
            config.minutos_minimos_aviso_enlace = 30
            config.save()
            self.assertEqual(notificar_cambios_enlaces()['caidos'], 0, 'con 30 todavia no')

            config.minutos_minimos_aviso_enlace = 5
            config.save()
            self.assertEqual(notificar_cambios_enlaces()['caidos'], 1, 'con 5 ya califica')

    def test_cambiar_las_fallas_consecutivas_cambia_cuando_se_declara_la_caida(self):
        from apps.monitoreo.enlaces import registrar_sondeo
        from apps.monitoreo.models import ConfiguracionMonitoreo, EstadoEnlaceFarmacia

        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX991')
        farmacia = Farmacia.objects.create(
            codigo='TSTQ02', grupo=grupo, unidad_negocio=sg, ip_router='10.6.0.2')

        config = ConfiguracionMonitoreo.obtener()
        config.fallas_consecutivas_enlace = 1
        config.save()

        registrar_sondeo(farmacia, True, 10.0)
        registrar_sondeo(farmacia, False, None)
        self.assertFalse(
            EstadoEnlaceFarmacia.objects.get(farmacia=farmacia).alcanzable,
            'con el umbral en 1, un solo fallo ya la declara caida')

    def test_cambiar_la_ventana_cambia_lo_que_muestra_toperrores(self):
        from apps.monitoreo.models import ConfiguracionMonitoreo, PosErrorDetectado
        from apps.monitoreo.telegram_bot import responder_a

        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX992')
        farmacia = Farmacia.objects.create(codigo='TSTQ03', grupo=grupo, unidad_negocio=sg)
        estacion = Estacion.objects.create(
            codigo='TSTQ03-A', farmacia=farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA)
        error = PosErrorDetectado.objects.create(
            estacion=estacion, mensaje='error de hace 20 dias', cantidad_total=50,
            categoria=PosErrorDetectado.Categoria.SISTEMA)
        PosErrorDetectado.objects.filter(pk=error.pk).update(
            ultima_vez=timezone.now() - timedelta(days=20))

        config = ConfiguracionMonitoreo.obtener()
        self.assertNotIn('error de hace 20 dias', responder_a('/toperrores'),
                         'con 7 dias queda afuera')

        config.dias_ventana_top_errores = 30
        config.save()
        self.assertIn('error de hace 20 dias', responder_a('/toperrores'),
                      'con 30 dias entra')

    def test_cambiar_el_escalamiento_cambia_que_alertas_se_reenvian(self):
        from apps.monitoreo.models import Alerta, ConfiguracionMonitoreo, Metrica, ReglaAlerta
        from apps.monitoreo.services import escalar_alertas_abiertas

        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX993')
        farmacia = Farmacia.objects.create(codigo='TSTQ04', grupo=grupo, unidad_negocio=sg)
        estacion = Estacion.objects.create(
            codigo='TSTQ04-A', farmacia=farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA)
        usuario = User.objects.create_user(username='u_cfg', password='x')
        regla = ReglaAlerta.objects.create(
            nombre='CPU alta', metrica=Metrica.CPU_CARGA_PCT, umbral=90, creado_por=usuario)
        alerta = Alerta.objects.create(regla=regla, estacion=estacion, valor_disparador=95)
        Alerta.objects.filter(pk=alerta.pk).update(
            abierta_en=timezone.now() - timedelta(minutes=45))

        config = ConfiguracionMonitoreo.obtener()
        config.minutos_escalamiento_alerta = 120
        config.save()
        self.assertEqual(escalar_alertas_abiertas(), 0, 'con 120 min todavia no toca')

        config.minutos_escalamiento_alerta = 30
        config.save()
        self.assertEqual(escalar_alertas_abiertas(), 1, 'con 30 min si')

    # --- el admin ---

    def test_el_admin_no_deja_crear_una_segunda_ni_borrar(self):
        from django.contrib.admin.sites import site

        from apps.monitoreo.models import ConfiguracionMonitoreo

        admin_config = site._registry[ConfiguracionMonitoreo]
        ConfiguracionMonitoreo.obtener()
        self.assertFalse(admin_config.has_add_permission(None))
        self.assertFalse(admin_config.has_delete_permission(None))

    def test_los_rangos_se_validan(self):
        """Poner 0 fallos consecutivos haria que cualquier paquete perdido sea una caida."""
        from django.core.exceptions import ValidationError

        from apps.monitoreo.models import ConfiguracionMonitoreo

        config = ConfiguracionMonitoreo.obtener()
        config.fallas_consecutivas_enlace = 0
        with self.assertRaises(ValidationError):
            config.full_clean()


class HistorialProtegidoTests(TestCase):
    """El historial de caidas no se puede perder por borrar su farmacia.

    Medido el 18-sep-2026: 690 de 700 farmacias no tenian ningun hijo con PROTECT (solo
    10 tienen estaciones), asi que el 99% del historial de SLA —1038 de 1046 eventos—
    dependia de que nadie tocara "eliminar" en el admin. El propio docstring de
    EventoEnlaceFarmacia decia que ese dato es la evidencia para discutir con el
    proveedor y que no se puede reconstruir.
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.grupo = Grupo.objects.create(codigo='TRX995')
        self.farmacia = Farmacia.objects.create(
            codigo='TSTH01', grupo=self.grupo, unidad_negocio=self.sg, ip_router='10.4.0.1')

    def test_no_se_puede_borrar_una_farmacia_con_historial(self):
        from django.db.models import ProtectedError

        from apps.monitoreo.models import EventoEnlaceFarmacia

        EventoEnlaceFarmacia.objects.create(
            farmacia=self.farmacia, inicio=timezone.now() - timedelta(hours=2))
        with self.assertRaises(ProtectedError):
            self.farmacia.delete()

    def test_el_historial_sobrevive_al_intento(self):
        """Lo que importa no es que falle, sino que el dato siga ahi despues."""
        from django.db.models import ProtectedError

        from apps.monitoreo.models import EventoEnlaceFarmacia

        EventoEnlaceFarmacia.objects.create(
            farmacia=self.farmacia, inicio=timezone.now() - timedelta(hours=2))
        try:
            self.farmacia.delete()
        except ProtectedError:
            pass
        self.assertEqual(EventoEnlaceFarmacia.objects.count(), 1)

    def test_una_farmacia_sin_historial_se_sigue_pudiendo_borrar(self):
        """PROTECT no puede convertirse en "nada se borra nunca": una farmacia cargada
        por error, que todavia no acumulo nada, tiene que poder eliminarse."""
        codigo = self.farmacia.pk
        self.farmacia.delete()
        self.assertFalse(Farmacia.objects.filter(pk=codigo).exists())

    def test_dar_de_baja_es_el_camino_y_conserva_el_historial(self):
        """`activa=False` es lo que el resto del sistema ya usa para una farmacia que
        deja de operar. No toca el historial, que es justamente el punto."""
        from apps.monitoreo.models import EventoEnlaceFarmacia

        EventoEnlaceFarmacia.objects.create(
            farmacia=self.farmacia, inicio=timezone.now() - timedelta(hours=2))
        self.farmacia.activa = False
        self.farmacia.save(update_fields=['activa'])

        self.farmacia.refresh_from_db()
        self.assertFalse(self.farmacia.activa)
        self.assertEqual(EventoEnlaceFarmacia.objects.filter(farmacia=self.farmacia).count(), 1)

    def test_el_historial_de_mantenimiento_tambien_esta_protegido(self):
        """Mantenimiento no tenia NINGUN PROTECT apuntandole: era mas facil de borrar que
        una farmacia."""
        from django.db.models import ProtectedError

        from apps.activos.models import Cargo, Colaborador, Departamento
        from apps.mantenimiento.models import EventoMantenimiento, Mantenimiento

        usuario = User.objects.create_user(username='u_prot', password='x')
        departamento = Departamento.objects.create(nombre='TI prueba')
        cargo = Cargo.objects.create(nombre='Tecnico prueba', departamento=departamento)
        colaborador = Colaborador.objects.create(
            nombre='Colaborador prueba', cedula='0999999999', cargo=cargo,
            unidad_negocio=self.sg,
        )
        mantenimiento = Mantenimiento.objects.create(
            cliente=colaborador, descripcion='Falla', fecha_programada=timezone.now(),
        )
        EventoMantenimiento.objects.create(
            mantenimiento=mantenimiento, tipo_evento=EventoMantenimiento.TipoEvento.PROGRAMADO,
            usuario=usuario, detalle={'origen': 'prueba'},
        )
        with self.assertRaises(ProtectedError):
            mantenimiento.delete()
        self.assertEqual(EventoMantenimiento.objects.count(), 1)

class AlertaDesfaseRelojTests(TestCase):
    """El reloj corrido tiene una ventana en la que se arregla solo y otra en la que
    obliga a un viaje. Estas pruebas fijan dónde está el corte.

    A los 120 s (VENTANA_TIMESTAMP_SEGUNDOS) el agente descarta TODO mensaje firmado,
    incluido el script que le arreglaría el reloj: pasada esa marca la estación solo se
    recupera yendo al local. Le pasó a MAM06-A el 26-ago-2026 y nadie se enteró hasta
    que ya estaba muda, porque el dato estaba en el panel y no había ninguna alerta.
    """

    def setUp(self):
        from apps.monitoreo.services import evaluar_regla_reloj

        self.evaluar = evaluar_regla_reloj
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX900')
        farmacia = Farmacia.objects.create(codigo='ML900', grupo=grupo, unidad_negocio=sg)
        self.estacion = Estacion.objects.create(
            codigo='ML900-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        usuario = User.objects.create_user(username='u_reloj', password='x')
        self.aviso = ReglaAlerta.objects.create(
            nombre='Reloj corrido (30 s)', metrica=Metrica.DESFASE_RELOJ,
            umbral=30, duracion_minutos=0, severidad=ReglaAlerta.Severidad.WARNING,
            creado_por=usuario,
        )
        self.critica = ReglaAlerta.objects.create(
            nombre='Reloj por quedar incomunicado (90 s)', metrica=Metrica.DESFASE_RELOJ,
            umbral=90, duracion_minutos=0, severidad=ReglaAlerta.Severidad.CRITICAL,
            creado_por=usuario,
        )

    def _con_desfase(self, segundos):
        self.estacion.desfase_reloj_segundos = segundos
        self.estacion.save(update_fields=['desfase_reloj_segundos'])
        self.evaluar(self.estacion)

    def test_un_reloj_en_hora_no_abre_nada(self):
        self._con_desfase(3)
        self.assertEqual(Alerta.objects.count(), 0)

    def test_sin_dato_de_reloj_no_abre_nada(self):
        """Una estación que todavía no reportó no es una estación con el reloj mal."""
        self.evaluar(self.estacion)
        self.assertEqual(Alerta.objects.count(), 0)

    def test_treinta_y_dos_segundos_abre_el_aviso_pero_no_la_critica(self):
        """El caso real de ML002-B el 19-sep-2026."""
        self._con_desfase(32)
        reglas = set(Alerta.objects.values_list('regla__nombre', flat=True))
        self.assertEqual(reglas, {'Reloj corrido (30 s)'})

    def test_una_estacion_ATRASADA_alerta_igual_que_una_adelantada(self):
        """El corazón de todo esto: el umbral se compara contra el ABSOLUTO.

        Un reloj 95 s atrasado rompe la firma HMAC igual que uno 95 s adelantado — el
        agente valida |ahora - timestamp|, no la dirección. Comparando el valor crudo
        con `gte`, -95 nunca superaría un umbral de 30 y la estación se quedaría muda
        sin que se abriera una sola alerta. Y atrasada es el caso MÁS común: es lo que
        hace un equipo con la pila de CMOS agotada.
        """
        self._con_desfase(-95)
        reglas = set(Alerta.objects.values_list('regla__nombre', flat=True))
        self.assertEqual(reglas, {'Reloj corrido (30 s)', 'Reloj por quedar incomunicado (90 s)'})

    def test_volver_a_la_hora_resuelve_la_alerta(self):
        self._con_desfase(60)
        self.assertEqual(Alerta.objects.filter(estado=Alerta.Estado.ABIERTA).count(), 1)

        self._con_desfase(2)

        alerta = Alerta.objects.get()
        self.assertEqual(alerta.estado, Alerta.Estado.RESUELTA)
        self.assertIsNotNone(alerta.resuelta_en)

    def test_se_guarda_el_desfase_CON_SIGNO_aunque_se_compare_el_absoluto(self):
        """La dirección dice la causa probable, así que no se puede perder."""
        self._con_desfase(-95)
        for alerta in Alerta.objects.all():
            self.assertEqual(alerta.valor_disparador, -95)

    def test_no_duplica_si_el_desfase_se_mantiene(self):
        self._con_desfase(60)
        self._con_desfase(61)
        self._con_desfase(62)
        self.assertEqual(Alerta.objects.count(), 1)

    def test_el_correo_dice_para_que_lado_y_cuanto_margen_queda(self):
        """'Valor: -95 (umbral: >= 90.0)' no le sirve a nadie a las 3 de la mañana."""
        PerfilUsuario.objects.create(usuario=self.aviso.creado_por, acceso_todas_unidades=True)
        self.aviso.creado_por.email = 'ops@example.com'
        self.aviso.creado_por.save(update_fields=['email'])
        self.critica.activo = False
        self.critica.save(update_fields=['activo'])

        self._con_desfase(-95)

        cuerpo = mail.outbox[0].body
        self.assertIn('atrasada 95 s', cuerpo)
        self.assertIn('25 s de margen', cuerpo)  # 120 - 95
        self.assertIn('ir al local', cuerpo)

    def test_el_correo_dice_adelantada_cuando_va_adelante(self):
        PerfilUsuario.objects.create(usuario=self.aviso.creado_por, acceso_todas_unidades=True)
        self.aviso.creado_por.email = 'ops@example.com'
        self.aviso.creado_por.save(update_fields=['email'])
        self.critica.activo = False
        self.critica.save(update_fields=['activo'])

        self._con_desfase(45)

        self.assertIn('adelantada 45 s', mail.outbox[0].body)

    @override_settings(TELEGRAM_BOT_TOKEN='TOKEN-DE-PRUEBA')
    def test_el_aviso_de_telegram_trae_el_boton_para_corregir(self):
        """Sin el botón, el aviso llega al teléfono y obliga a abrir la computadora."""
        CanalNotificacion.objects.create(
            tipo=CanalNotificacion.Tipo.TELEGRAM, destino='999', activo=True,
            creado_por=self.aviso.creado_por,
        )
        self.critica.activo = False
        self.critica.save(update_fields=['activo'])

        with patch('apps.monitoreo.services._enviar_telegram', return_value=True) as enviar:
            self._con_desfase(45)

        self.assertEqual(enviar.call_count, 1)
        teclado = enviar.call_args.kwargs['teclado']
        botones = [b for fila in teclado for b in fila]
        self.assertEqual(botones[0]['callback_data'], 'pedirsync:ML900-A')
        self.assertIn('ML900-A', botones[0]['text'])

    @override_settings(TELEGRAM_BOT_TOKEN='TOKEN-DE-PRUEBA')
    def test_las_otras_alertas_siguen_sin_boton(self):
        """El botón es la excepción, no el nuevo default de toda notificación."""
        from apps.monitoreo.services import abrir_o_mantener_alerta

        CanalNotificacion.objects.create(
            tipo=CanalNotificacion.Tipo.TELEGRAM, destino='999', activo=True,
            creado_por=self.aviso.creado_por,
        )
        regla_disco = ReglaAlerta.objects.create(
            nombre='Disco lleno', metrica=Metrica.DISCO_USADO_PCT, umbral=95,
            duracion_minutos=0, creado_por=self.aviso.creado_por,
        )

        with patch('apps.monitoreo.services._enviar_telegram', return_value=True) as enviar:
            abrir_o_mantener_alerta(regla_disco, self.estacion, 99)

        self.assertEqual(enviar.call_count, 1)
        self.assertIsNone(enviar.call_args.kwargs['teclado'])


class SincronizarHoraPorTelegramTests(TestCase):
    """La primera acción de escritura del bot. Lo que se prueba acá es sobre todo lo que
    NO tiene que poder hacerse: hasta ahora el peor caso de un chat comprometido era leer
    el mapa de la red, y desde este comando pasa a ser accionar sobre una caja.
    """

    def setUp(self):
        from apps.scripts.management.commands.seed_scripts_hora import NOMBRE_SINCRONIZAR
        from apps.scripts.models import Script, TipoScript

        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        grupo = Grupo.objects.create(codigo='TRX901')
        farmacia = Farmacia.objects.create(codigo='ML901', grupo=grupo, unidad_negocio=self.sg)
        self.estacion = Estacion.objects.create(
            codigo='ML901-A', farmacia=farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA, desfase_reloj_segundos=40,
        )

        self.admin = User.objects.create_user(username='admin_h', password='x', is_superuser=True)
        self.script = Script.objects.create(
            nombre=NOMBRE_SINCRONIZAR, tipo=TipoScript.POWERSHELL, contenido='w32tm /resync',
            categoria='Hora', unidad_negocio=None, creado_por=self.admin,
        )

        # Quien sí puede: permiso de ejecutar scripts y acceso a SG.
        self.operador = User.objects.create_user(username='operador', password='x')
        self.operador.user_permissions.add(
            Permission.objects.get(content_type__app_label='scripts', codename='add_ejecucionscript'),
        )
        perfil = PerfilUsuario.objects.create(
            usuario=self.operador, acceso_todas_unidades=False, telegram_chat_id='111',
        )
        perfil.unidades_negocio.add(self.sg)

    def _sincronizar(self, codigo, chat_id):
        from apps.monitoreo.telegram_bot import _ejecutar_sincronizar

        return _ejecutar_sincronizar(codigo, chat_id)

    def test_un_chat_sin_usuario_atado_no_puede_accionar(self):
        """Estar en TELEGRAM_CHAT_IDS_AUTORIZADOS alcanza para consultar, no para esto."""
        from apps.scripts.models import EjecucionScript

        respuesta = self._sincronizar('ML901-A', '999')

        self.assertEqual(EjecucionScript.objects.count(), 0)
        self.assertIn('no accionar', respuesta)

    def test_el_mensaje_explica_como_habilitarse_y_dice_el_chat_id(self):
        """Un "no autorizado" a secas deja a la persona sin saber qué pedir."""
        respuesta = self._sincronizar('ML901-A', '999')
        self.assertIn('999', respuesta)

    def test_sin_el_permiso_de_ejecutar_scripts_no_puede(self):
        from apps.scripts.models import EjecucionScript

        miron = User.objects.create_user(username='miron', password='x')
        PerfilUsuario.objects.create(
            usuario=miron, acceso_todas_unidades=True, telegram_chat_id='222',
        )

        respuesta = self._sincronizar('ML901-A', '222')

        self.assertEqual(EjecucionScript.objects.count(), 0)
        self.assertIn('permiso', respuesta)

    def test_no_puede_accionar_sobre_una_unidad_de_negocio_ajena(self):
        """El mismo aislamiento multi-tenant del panel, no uno paralelo."""
        from apps.scripts.models import EjecucionScript

        ajeno = User.objects.create_user(username='ajeno', password='x')
        ajeno.user_permissions.add(
            Permission.objects.get(content_type__app_label='scripts', codename='add_ejecucionscript'),
        )
        perfil = PerfilUsuario.objects.create(
            usuario=ajeno, acceso_todas_unidades=False, telegram_chat_id='333',
        )
        perfil.unidades_negocio.add(self.mia)

        respuesta = self._sincronizar('ML901-A', '333')

        self.assertEqual(EjecucionScript.objects.count(), 0)
        self.assertIn('SG', respuesta)

    def test_un_usuario_desactivado_no_puede_accionar(self):
        """Dar de baja a alguien en Django tiene que apagarle también el Telegram."""
        from apps.scripts.models import EjecucionScript

        self.operador.is_active = False
        self.operador.save(update_fields=['is_active'])

        self._sincronizar('ML901-A', '111')

        self.assertEqual(EjecucionScript.objects.count(), 0)

    def test_quien_tiene_todo_si_dispara_y_queda_A_SU_NOMBRE(self):
        from apps.scripts.models import EjecucionScript

        with patch('apps.scripts.services.enviar_script', return_value=True):
            respuesta = self._sincronizar('ML901-A', '111')

        ejecucion = EjecucionScript.objects.get()
        self.assertEqual(ejecucion.creado_por, self.operador)
        self.assertEqual(ejecucion.script, self.script)
        self.assertEqual(list(ejecucion.estaciones.all()), [self.estacion])
        self.assertIn('operador', respuesta)

    def test_una_estacion_inexistente_no_revienta(self):
        respuesta = self._sincronizar('NO-EXISTE', '111')
        self.assertIn('No encuentro', respuesta)

    def test_sin_el_script_sembrado_lo_dice_en_vez_de_fallar_raro(self):
        self.script.delete()
        respuesta = self._sincronizar('ML901-A', '111')
        self.assertIn('seed_scripts_hora', respuesta)

    def test_escribir_el_comando_NO_ejecuta_solo_pide_confirmacion(self):
        """Accionar sobre una estación no puede salir de un solo tipeo."""
        from apps.monitoreo.telegram_bot import responder_a
        from apps.scripts.models import EjecucionScript

        respuesta = responder_a('/sincronizar ML901-A', '111')

        self.assertEqual(EjecucionScript.objects.count(), 0)
        texto, teclado = respuesta
        botones = [b for fila in teclado for b in fila]
        self.assertEqual(botones[0]['callback_data'], 'sync:ML901-A')

    def test_la_confirmacion_avisa_si_la_estacion_ya_esta_incomunicada(self):
        """Con >120 s el agente descarta el script: hay que decirlo antes, no dejar que
        la persona crea que quedó resuelto."""
        from apps.monitoreo.telegram_bot import responder_a

        self.estacion.desfase_reloj_segundos = 300
        self.estacion.save(update_fields=['desfase_reloj_segundos'])

        texto, _ = responder_a('/sincronizar ML901-A', '111')

        self.assertIn('DESCARTA', texto)
        self.assertIn('ir al local', texto)

    def test_el_boton_de_la_alerta_pide_confirmacion_no_ejecuta(self):
        from apps.monitoreo.telegram_bot import responder_a_callback
        from apps.scripts.models import EjecucionScript

        resultado = responder_a_callback('pedirsync:ML901-A', '111')

        self.assertEqual(EjecucionScript.objects.count(), 0)
        texto, _ = resultado
        self.assertIn('ML901-A', texto)

    def test_confirmar_desde_un_chat_sin_usuario_tampoco_ejecuta(self):
        """La confirmación no es la autorización: se vuelve a chequear al ejecutar."""
        from apps.monitoreo.telegram_bot import responder_a_callback
        from apps.scripts.models import EjecucionScript

        responder_a_callback('sync:ML901-A', '999')

        self.assertEqual(EjecucionScript.objects.count(), 0)

    def test_sincronizar_sin_codigo_pide_el_codigo(self):
        from apps.monitoreo.telegram_bot import responder_a

        self.assertIn('/sincronizar CODIGO', responder_a('/sincronizar', '111'))

    def test_hora_lista_las_corridas_y_no_las_que_estan_en_hora(self):
        from apps.monitoreo.telegram_bot import _comando_hora

        grupo = Grupo.objects.create(codigo='TRX902')
        farmacia = Farmacia.objects.create(codigo='ML902', grupo=grupo, unidad_negocio=self.sg)
        Estacion.objects.create(
            codigo='ML902-A', farmacia=farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA, desfase_reloj_segundos=1,
        )

        salida = _comando_hora()

        self.assertIn('ML901-A', salida)
        self.assertNotIn('ML902-A', salida)
