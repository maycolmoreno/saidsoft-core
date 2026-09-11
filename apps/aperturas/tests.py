from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from apps.catalogo.models import Estacion, Farmacia, Grupo, UnidadNegocio
from apps.mqtt_worker.services import manejar_enrolamiento
from apps.scripts.models import Script
from apps.software.models import SoftwareInstaladoDetectado

from .models import (
    Apertura,
    EventoApertura,
    PasoApertura,
    PasoPlantilla,
    PerfilEstacionPlantilla,
    PlantillaApertura,
    TipoPaso,
    TipoVerificacion,
    TokenApertura,
    hashear_token,
)
from .services import (
    aprobar_apertura,
    completar_paso_manual,
    consumir_token,
    crear_apertura,
    emitir_tokens,
    evaluar_verificacion,
    recalcular_estado_apertura,
    revocar_token,
)


class _BaseAperturaTests(TestCase):
    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.grupo = Grupo.objects.create(codigo='TRX001', version_objetivo='4.2.1')
        self.farmacia = Farmacia.objects.create(codigo='ML099', grupo=self.grupo, unidad_negocio=self.sg)
        self.creador = User.objects.create_user(username='creador', password='clave-larga-1')
        self.aprobador = User.objects.create_user(username='aprobador', password='clave-larga-2')

        self.plantilla = PlantillaApertura.objects.create(
            nombre='Mostrador estándar', unidad_negocio=self.sg, creado_por=self.creador,
        )
        self.perfil_adm = PerfilEstacionPlantilla.objects.create(
            plantilla=self.plantilla, sufijo='ADM', rol=PerfilEstacionPlantilla.Rol.SERVIDOR,
            monitorear_recursos=True, es_cache_farmacia=True,
        )
        self.perfil_caja = PerfilEstacionPlantilla.objects.create(
            plantilla=self.plantilla, sufijo='A', rol=PerfilEstacionPlantilla.Rol.CAJA,
        )
        self.script = Script.objects.create(
            nombre='Configurar energía', contenido='Write-Output ok', creado_por=self.creador,
        )
        self.paso_script = PasoPlantilla.objects.create(
            plantilla=self.plantilla, orden=10, nombre='Configurar energía',
            tipo=TipoPaso.SCRIPT, script=self.script,
        )
        self.paso_manual = PasoPlantilla.objects.create(
            plantilla=self.plantilla, orden=20, nombre='Alta en Active Directory', tipo=TipoPaso.MANUAL,
        )

    def _apertura_aprobada(self):
        apertura = crear_apertura(
            farmacia=self.farmacia, plantilla=self.plantilla,
            fecha_prevista=timezone.localdate(), usuario=self.creador,
        )
        return aprobar_apertura(apertura=apertura, usuario=self.aprobador)

    def _token_para(self, perfil):
        apertura = self._apertura_aprobada()
        emitidos = emitir_tokens(apertura=apertura, usuario=self.aprobador)
        for p, plano in emitidos:
            if p.pk == perfil.pk:
                return apertura, plano
        raise AssertionError('No se emitió token para ese perfil')


class CrearAperturaTests(_BaseAperturaTests):
    def test_materializa_solo_los_pasos_manuales(self):
        """Los pasos de estación no se pueden materializar al crear: las estaciones de una
        farmacia nueva todavía no existen en la base."""
        apertura = crear_apertura(
            farmacia=self.farmacia, plantilla=self.plantilla,
            fecha_prevista=timezone.localdate(), usuario=self.creador,
        )
        self.assertEqual(apertura.estado, Apertura.Estado.PENDIENTE_APROBACION)
        self.assertEqual([p.tipo for p in apertura.pasos.all()], [TipoPaso.MANUAL])
        self.assertIsNone(apertura.pasos.first().estacion)

    def test_rechaza_plantilla_de_otra_unidad_de_negocio(self):
        mia = UnidadNegocio.objects.get(codigo='MIA')
        ajena = PlantillaApertura.objects.create(
            nombre='Otra', unidad_negocio=mia, creado_por=self.creador,
        )
        with self.assertRaisesMessage(ValueError, 'no cruza unidades de negocio'):
            crear_apertura(
                farmacia=self.farmacia, plantilla=ajena,
                fecha_prevista=timezone.localdate(), usuario=self.creador,
            )

    def test_no_permite_dos_aperturas_vigentes_en_la_misma_farmacia(self):
        crear_apertura(
            farmacia=self.farmacia, plantilla=self.plantilla,
            fecha_prevista=timezone.localdate(), usuario=self.creador,
        )
        with self.assertRaisesMessage(ValueError, 'ya tiene una apertura vigente'):
            crear_apertura(
                farmacia=self.farmacia, plantilla=self.plantilla,
                fecha_prevista=timezone.localdate(), usuario=self.creador,
            )


class AprobacionTests(_BaseAperturaTests):
    def test_quien_crea_no_puede_aprobar(self):
        apertura = crear_apertura(
            farmacia=self.farmacia, plantilla=self.plantilla,
            fecha_prevista=timezone.localdate(), usuario=self.creador,
        )
        with self.assertRaisesMessage(ValueError, 'cuatro ojos'):
            aprobar_apertura(apertura=apertura, usuario=self.creador)

    def test_no_se_emiten_tokens_sin_aprobar(self):
        """El token auto-aprueba estaciones: emitirlo antes de la aprobación saltearía
        justamente el control que la aprobación representa."""
        apertura = crear_apertura(
            farmacia=self.farmacia, plantilla=self.plantilla,
            fecha_prevista=timezone.localdate(), usuario=self.creador,
        )
        with self.assertRaisesMessage(ValueError, 'apertura aprobada'):
            emitir_tokens(apertura=apertura, usuario=self.aprobador)


class TokenAperturaTests(_BaseAperturaTests):
    def test_el_token_en_claro_no_se_persiste(self):
        """§10-Z: el paquete anterior llevaba los secretos compartidos adentro. Acá solo
        se guarda el hash — el valor en claro existe una vez y no se puede reconsultar."""
        apertura = self._apertura_aprobada()
        emitidos = emitir_tokens(apertura=apertura, usuario=self.aprobador)
        self.assertEqual(len(emitidos), 2)
        for _, plano in emitidos:
            self.assertFalse(TokenApertura.objects.filter(token_hash=plano).exists())
            token = TokenApertura.objects.get(token_hash=hashear_token(plano))
            self.assertEqual(token.prefijo, plano[:8])

    def test_no_reemite_para_un_perfil_que_ya_tiene_token_vigente(self):
        apertura = self._apertura_aprobada()
        emitir_tokens(apertura=apertura, usuario=self.aprobador)
        self.assertEqual(emitir_tokens(apertura=apertura, usuario=self.aprobador), [])
        self.assertEqual(apertura.tokens.count(), 2)

    def test_reemite_si_el_token_fue_revocado(self):
        apertura = self._apertura_aprobada()
        emitir_tokens(apertura=apertura, usuario=self.aprobador)
        revocar_token(token=apertura.tokens.first(), usuario=self.aprobador, motivo='se filtró')
        self.assertEqual(len(emitir_tokens(apertura=apertura, usuario=self.aprobador)), 1)

    def test_rechaza_token_desconocido_vencido_usado_o_revocado(self):
        apertura, plano = self._token_para(self.perfil_caja)
        self.assertIsNone(consumir_token(
            token_plano='no-existe', codigo_estacion='ML099-A', hardware_id='hw',
        ))

        token = TokenApertura.objects.get(token_hash=hashear_token(plano))
        for campo, valor, motivo in (
            ('expira_en', timezone.now() - timedelta(days=1), 'vencido'),
            ('usado_en', timezone.now(), 'ya usado'),
            ('revocado', True, 'revocado'),
        ):
            original = getattr(token, campo)
            setattr(token, campo, valor)
            token.save(update_fields=[campo])
            self.assertEqual(token.motivo_no_vigente, motivo)
            self.assertIsNone(consumir_token(
                token_plano=plano, codigo_estacion='ML099-A', hardware_id='hw',
            ))
            setattr(token, campo, original)
            token.save(update_fields=[campo])

    def test_rechaza_token_usado_en_otra_farmacia(self):
        """Sin esto, un token filtrado sirve para enrolar un equipo en cualquier farmacia."""
        Farmacia.objects.create(codigo='ML100', grupo=self.grupo, unidad_negocio=self.sg)
        _, plano = self._token_para(self.perfil_caja)
        self.assertIsNone(consumir_token(
            token_plano=plano, codigo_estacion='ML100-A', hardware_id='hw',
        ))

    def test_rechaza_token_usado_con_otro_sufijo(self):
        """Un token emitido para la caja -A no puede enrolar un equipo que dice ser el
        servidor -ADM y llevarse sus flags (monitoreo, caché de farmacia)."""
        _, plano = self._token_para(self.perfil_caja)
        self.assertIsNone(consumir_token(
            token_plano=plano, codigo_estacion='ML099-ADM', hardware_id='hw',
        ))

    def test_acepta_el_token_correcto(self):
        _, plano = self._token_para(self.perfil_caja)
        token = consumir_token(token_plano=plano, codigo_estacion='ML099-A', hardware_id='hw')
        self.assertIsNotNone(token)
        self.assertEqual(token.perfil, self.perfil_caja)


class EnrolamientoCeroTouchTests(_BaseAperturaTests):
    """El camino completo: un equipo enchufado en el local se presenta con su token y
    queda aprobado, configurado y con sus pasos lanzados sin que nadie lo toque."""

    def _payload(self, codigo, token_plano=None):
        payload = {
            'codigo': codigo, 'hardware_id': f'hw-{codigo}', 'hostname': codigo,
            'numero_serie': f'SN-{codigo}', 'so_nombre': 'Windows 11', 'so_build': '22631',
            'version_agente': '1.0.0',
        }
        if token_plano:
            payload['token_apertura'] = token_plano
        return payload

    @patch('apps.scripts.services.enviar_script', return_value=True)
    @patch('apps.mqtt_worker.services.aprovisionar_credencial_estacion', return_value=None)
    def test_enrola_aprobada_con_la_configuracion_del_perfil(self, _cred, _enviar):
        apertura, plano = self._token_para(self.perfil_adm)

        respuesta = manejar_enrolamiento(self._payload('ML099-ADM', plano))

        self.assertTrue(respuesta['aceptado'])
        estacion = Estacion.objects.get(codigo='ML099-ADM')
        self.assertEqual(estacion.estado_aprobacion, Estacion.EstadoAprobacion.APROBADA)
        # Los dos flags que hoy alguien tilda a mano después de cada instalación.
        self.assertTrue(estacion.monitorear_recursos)
        self.assertTrue(estacion.es_cache_farmacia)
        self.assertEqual(estacion.numero_serie, 'SN-ML099-ADM')

        apertura.refresh_from_db()
        self.assertEqual(apertura.estado, Apertura.Estado.EN_CURSO)

        token = TokenApertura.objects.get(token_hash=hashear_token(plano))
        self.assertIsNotNone(token.usado_en)
        self.assertEqual(token.estacion, estacion)
        self.assertEqual(token.hardware_id, 'hw-ML099-ADM')

    @patch('apps.scripts.services.enviar_script', return_value=True)
    @patch('apps.mqtt_worker.services.aprovisionar_credencial_estacion', return_value=None)
    def test_el_token_no_se_puede_reusar(self, _cred, _enviar):
        _, plano = self._token_para(self.perfil_caja)
        manejar_enrolamiento(self._payload('ML099-A', plano))

        # Otro equipo intentando el mismo token: cae al camino normal. No puede crear la
        # estación (el código ya existe) y responde el re-enrolamiento rechazado por
        # hardware_id distinto, que es exactamente la protección que ya existía.
        respuesta = manejar_enrolamiento({
            'codigo': 'ML099-A', 'hardware_id': 'hw-intruso', 'token_apertura': plano,
        })
        self.assertFalse(respuesta['aceptado'])

    @patch('apps.scripts.services.enviar_script', return_value=True)
    @patch('apps.mqtt_worker.services.aprovisionar_credencial_estacion', return_value=None)
    def test_lanza_los_pasos_de_la_plantilla(self, _cred, mock_enviar):
        apertura, plano = self._token_para(self.perfil_caja)
        manejar_enrolamiento(self._payload('ML099-A', plano))

        estacion = Estacion.objects.get(codigo='ML099-A')
        pasos = list(apertura.pasos.filter(estacion=estacion))
        self.assertEqual([p.tipo for p in pasos], [TipoPaso.SCRIPT])
        self.assertIsNotNone(pasos[0].ejecucion_script)
        self.assertEqual(pasos[0].estado, PasoApertura.Estado.EN_CURSO)
        mock_enviar.assert_called_once()

    @patch('apps.mqtt_worker.services.aprovisionar_credencial_estacion', return_value=None)
    def test_sin_token_sigue_quedando_pendiente_de_aprobacion(self, _cred):
        """El camino de siempre no cambia: cero-touch es una vía nueva, no un reemplazo."""
        self._apertura_aprobada()
        respuesta = manejar_enrolamiento(self._payload('ML099-A'))

        self.assertTrue(respuesta['aceptado'])
        estacion = Estacion.objects.get(codigo='ML099-A')
        self.assertEqual(estacion.estado_aprobacion, Estacion.EstadoAprobacion.PENDIENTE)
        self.assertFalse(estacion.monitorear_recursos)

    @patch('apps.mqtt_worker.services.aprovisionar_credencial_estacion', return_value=None)
    def test_un_token_invalido_no_bloquea_el_enrolamiento_manual(self, _cred):
        """Un token mal copiado en el config.txt no puede dejar al técnico sin poder
        enrolar nada: cae al camino normal, pendiente de aprobación."""
        self._apertura_aprobada()
        respuesta = manejar_enrolamiento(self._payload('ML099-A', 'token-basura'))

        self.assertTrue(respuesta['aceptado'])
        self.assertEqual(
            Estacion.objects.get(codigo='ML099-A').estado_aprobacion,
            Estacion.EstadoAprobacion.PENDIENTE,
        )

    @patch('apps.scripts.services.enviar_script', return_value=True)
    @patch('apps.mqtt_worker.services.aprovisionar_credencial_estacion', return_value=None)
    def test_queda_rastro_auditable_de_la_aprobacion_automatica(self, _cred, _enviar):
        """Entrar sin el clic de un humano no puede significar entrar sin que nadie se
        entere: queda el evento de auditoría con el token que la autorizó."""
        from apps.auditoria.models import EventoAuditoria

        _, plano = self._token_para(self.perfil_caja)
        manejar_enrolamiento(self._payload('ML099-A', plano))

        evento = EventoAuditoria.objects.get(accion='apertura.estacion_enrolada')
        self.assertEqual(evento.objeto_repr, 'ML099-A')
        self.assertTrue(evento.detalle['aprobada_automaticamente'])
        self.assertEqual(evento.detalle['token_prefijo'], plano[:8])
        self.assertEqual(evento.unidad_negocio, self.sg)


class VerificacionTests(_BaseAperturaTests):
    def setUp(self):
        super().setUp()
        self.estacion = Estacion.objects.create(
            codigo='ML099-A', farmacia=self.farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )

    def test_bitlocker_distingue_sin_reportar_de_no_cifrado(self):
        """null no es lo mismo que False: "todavía no se consultó" y "el disco está sin
        cifrar" llevan a acciones distintas en el sitio."""
        cumple, detalle = evaluar_verificacion(estacion=self.estacion, tipo=TipoVerificacion.BITLOCKER)
        self.assertFalse(cumple)
        self.assertIn('sin reportar', detalle)

        self.estacion.bitlocker_habilitado = False
        self.estacion.save(update_fields=['bitlocker_habilitado'])
        cumple, detalle = evaluar_verificacion(estacion=self.estacion, tipo=TipoVerificacion.BITLOCKER)
        self.assertFalse(cumple)
        self.assertIn('no está cifrado', detalle)

        self.estacion.bitlocker_habilitado = True
        self.estacion.bitlocker_metodo_proteccion = 'tpm'
        self.estacion.save(update_fields=['bitlocker_habilitado', 'bitlocker_metodo_proteccion'])
        cumple, _ = evaluar_verificacion(estacion=self.estacion, tipo=TipoVerificacion.BITLOCKER)
        self.assertTrue(cumple)

    def test_software_presente_exige_un_escaneo_previo(self):
        cumple, detalle = evaluar_verificacion(
            estacion=self.estacion, tipo=TipoVerificacion.SOFTWARE_PRESENTE, parametro='ESET',
        )
        self.assertFalse(cumple)
        self.assertIn('Falta escanear', detalle)

        self.estacion.software_instalado_ultima_verificacion = timezone.now()
        self.estacion.save(update_fields=['software_instalado_ultima_verificacion'])
        SoftwareInstaladoDetectado.objects.create(
            estacion=self.estacion, nombre='ESET Endpoint Security', version='10.1',
        )
        cumple, detalle = evaluar_verificacion(
            estacion=self.estacion, tipo=TipoVerificacion.SOFTWARE_PRESENTE, parametro='ESET',
        )
        self.assertTrue(cumple)
        self.assertIn('ESET Endpoint Security', detalle)

    def test_version_pos_objetivo(self):
        cumple, detalle = evaluar_verificacion(
            estacion=self.estacion, tipo=TipoVerificacion.VERSION_POS_OBJETIVO,
        )
        self.assertFalse(cumple)
        self.assertIn('no reportó', detalle)

        self.estacion.version_pos = '4.2.1'
        self.estacion.save(update_fields=['version_pos'])
        cumple, _ = evaluar_verificacion(
            estacion=self.estacion, tipo=TipoVerificacion.VERSION_POS_OBJETIVO,
        )
        self.assertTrue(cumple)


class CierreDeAperturaTests(_BaseAperturaTests):
    @patch('apps.scripts.services.enviar_script', return_value=True)
    @patch('apps.mqtt_worker.services.aprovisionar_credencial_estacion', return_value=None)
    def test_no_completa_si_falta_una_estacion_obligatoria(self, _cred, _enviar):
        """Una farmacia cuyo servidor nunca apareció no está abierta, por más que la caja
        que sí llegó haya terminado todos sus pasos."""
        apertura, plano = self._token_para(self.perfil_caja)
        manejar_enrolamiento({
            'codigo': 'ML099-A', 'hardware_id': 'hw-a', 'token_apertura': plano,
        })
        for paso in apertura.pasos.all():
            paso.estado = PasoApertura.Estado.COMPLETADO
            paso.save(update_fields=['estado'])

        recalcular_estado_apertura(apertura)
        apertura.refresh_from_db()
        self.assertEqual(apertura.estado, Apertura.Estado.EN_CURSO)

    @patch('apps.scripts.services.enviar_script', return_value=True)
    @patch('apps.mqtt_worker.services.aprovisionar_credencial_estacion', return_value=None)
    def test_completa_y_fecha_la_apertura_de_la_farmacia(self, _cred, _enviar):
        apertura = self._apertura_aprobada()
        emitidos = emitir_tokens(apertura=apertura, usuario=self.aprobador)
        self.assertEqual(len(emitidos), 2)
        for perfil, plano in emitidos:
            manejar_enrolamiento({
                'codigo': f'ML099-{perfil.sufijo}', 'hardware_id': f'hw-{perfil.sufijo}',
                'token_apertura': plano,
            })

        for paso in apertura.pasos.filter(tipo=TipoPaso.SCRIPT):
            paso.estado = PasoApertura.Estado.COMPLETADO
            paso.save(update_fields=['estado'])
        completar_paso_manual(
            paso=apertura.pasos.get(tipo=TipoPaso.MANUAL), usuario=self.aprobador, detalle='AD listo',
        )

        apertura.refresh_from_db()
        self.assertEqual(apertura.estado, Apertura.Estado.COMPLETADA)
        self.farmacia.refresh_from_db()
        self.assertEqual(self.farmacia.fecha_apertura, timezone.localdate())


class EventoAperturaTests(_BaseAperturaTests):
    def test_el_evento_es_inmutable(self):
        apertura = crear_apertura(
            farmacia=self.farmacia, plantilla=self.plantilla,
            fecha_prevista=timezone.localdate(), usuario=self.creador,
        )
        evento = EventoApertura.objects.filter(paso__apertura=apertura).first()
        self.assertIsNotNone(evento)
        with self.assertRaises(NotImplementedError):
            evento.delete()
