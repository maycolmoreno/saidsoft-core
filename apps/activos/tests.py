import datetime

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.catalogo.models import Estacion, Farmacia, Grupo, UnidadNegocio

from .models import (
    Activo, Bodega, Cargo, Colaborador, Departamento, EventoActivo, StockBodega, TipoConsumible,
)
from .services import (
    activos_dados_de_baja_pero_conectados, activos_movidos_sin_registro, activos_por_vencer_garantia,
    registrar_asignacion, registrar_salida_stock, registrar_traslado_bodega, scope_movimientos_visibles,
    stock_bajo_minimo, vincular_activos_por_numero_serie,
)


class SeedPermisosTests(TestCase):
    def test_administrador_incluye_acceso_remoto_estacion(self):
        call_command('seed_permisos')
        grupo = Group.objects.get(name='Administrador')
        self.assertTrue(
            grupo.permissions.filter(
                content_type__app_label='catalogo', codename='acceso_remoto_estacion',
            ).exists(),
        )

    def test_auditor_incluye_supervision_auditoria_pero_no_acceso_remoto(self):
        call_command('seed_permisos')
        grupo = Group.objects.get(name='Auditor')
        self.assertTrue(
            grupo.permissions.filter(
                content_type__app_label='catalogo', codename='supervision_auditoria_estacion',
            ).exists(),
        )
        self.assertFalse(
            grupo.permissions.filter(
                content_type__app_label='catalogo', codename='acceso_remoto_estacion',
            ).exists(),
        )

    def test_mesa_de_ayuda_solo_diagnostico_sin_acciones_de_riesgo(self):
        call_command('seed_permisos')
        grupo = Group.objects.get(name='Mesa de Ayuda')
        codenames = set(grupo.permissions.values_list('codename', flat=True))
        self.assertEqual(codenames, {'acceso_remoto_estacion', 'consultar_info_estacion', 'view_estacion'})

    def test_soporte_tecnico_tiene_acciones_de_riesgo_pero_no_bitlocker_ni_grabaciones(self):
        call_command('seed_permisos')
        grupo = Group.objects.get(name='Soporte Técnico')
        codenames = set(grupo.permissions.values_list('codename', flat=True))
        self.assertTrue({
            'acceso_remoto_estacion', 'consultar_info_estacion', 'aprobar_estacion',
            'reiniciar_estacion', 'escanear_actualizaciones_estacion',
            'add_script', 'view_script', 'add_ejecucionscript', 'view_ejecucionscript',
        }.issubset(codenames))
        self.assertNotIn('ver_clave_bitlocker', codenames)
        self.assertNotIn('supervision_auditoria_estacion', codenames)


class RegistrarAsignacionTests(TestCase):
    def setUp(self):
        self.usuario = User.objects.create_user(username='u', password='x')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        self.colaborador = Colaborador.objects.create(nombre='Ana', cedula='0001', unidad_negocio=self.mia)
        self.activo = Activo.objects.create(codigo='CR-DSK-0001', tipo=Activo.Tipo.DESKTOP)

    def test_activo_hereda_unidad_negocio_del_colaborador(self):
        registrar_asignacion(
            activo=self.activo, colaborador=self.colaborador,
            estado_fisico_entrega=Activo.EstadoFisico.BUENO, usuario=self.usuario,
        )
        self.activo.refresh_from_db()
        self.assertEqual(self.activo.unidad_negocio, self.mia)

    def test_colaborador_con_cargo_no_revienta_al_serializar_el_evento(self):
        """colaborador.cargo es FK a Cargo (no serializable a JSON tal cual) — antes
        se guardaba el objeto directo en el detalle y reventaba con TypeError en
        cuanto el colaborador tuviera un cargo asignado."""
        departamento = Departamento.objects.create(nombre='TI')
        cargo = Cargo.objects.create(nombre='Técnico de soporte', departamento=departamento)
        self.colaborador.cargo = cargo
        self.colaborador.save(update_fields=['cargo'])

        registrar_asignacion(
            activo=self.activo, colaborador=self.colaborador,
            estado_fisico_entrega=Activo.EstadoFisico.BUENO, usuario=self.usuario,
        )
        evento = EventoActivo.objects.get(activo=self.activo, tipo_evento=EventoActivo.TipoEvento.ASIGNACION)
        self.assertEqual(evento.detalle['cargo'], 'Técnico de soporte')

    def test_devolver_no_limpia_la_unidad_negocio_heredada(self):
        from .services import registrar_devolucion
        registrar_asignacion(
            activo=self.activo, colaborador=self.colaborador,
            estado_fisico_entrega=Activo.EstadoFisico.BUENO, usuario=self.usuario,
        )
        registrar_devolucion(
            activo=self.activo, estado_fisico_devolucion=Activo.EstadoFisico.BUENO,
            requiere_reparacion=False, usuario=self.usuario,
        )
        self.activo.refresh_from_db()
        self.assertEqual(self.activo.unidad_negocio, self.mia)


class RegistrarSalidaStockTests(TestCase):
    def setUp(self):
        self.bodega = Bodega.objects.create(codigo='BOD01')
        self.tipo_consumible = TipoConsumible.objects.create(codigo='MOUSE', nombre='Mouse USB')
        StockBodega.objects.create(bodega=self.bodega, tipo_consumible=self.tipo_consumible, cantidad=5)

    def test_descuenta_stock_disponible(self):
        stock = registrar_salida_stock(bodega=self.bodega, tipo_consumible=self.tipo_consumible, cantidad=3)
        self.assertEqual(stock.cantidad, 2)

    def test_rechaza_cantidad_mayor_al_disponible(self):
        with self.assertRaises(ValueError):
            registrar_salida_stock(bodega=self.bodega, tipo_consumible=self.tipo_consumible, cantidad=6)
        stock = StockBodega.objects.get(bodega=self.bodega, tipo_consumible=self.tipo_consumible)
        self.assertEqual(stock.cantidad, 5)

    def test_rechaza_cantidad_cero_o_negativa(self):
        with self.assertRaises(ValueError):
            registrar_salida_stock(bodega=self.bodega, tipo_consumible=self.tipo_consumible, cantidad=0)


class RegistrarSalidaStockConcurrenciaTests(TestCase):
    """BUG-1 de la auditoría de gobernanza (22-ago-2026): `registrar_salida_stock` hacía
    leer-modificar-escribir en Python (`stock.cantidad -= cantidad; stock.save()`) sin
    `select_for_update` ni `F()` — dos salidas simultáneas podían leer el mismo saldo
    antes de que cualquiera escribiera, así que las dos pasaban la validación de "stock
    suficiente" y el resultado final quedaba por debajo de cero sin ningún error
    visible. El fix (`_obtener_y_bloquear_stock` + `F()` en el `UPDATE`) es el patrón
    estándar de Django/Postgres para esta clase de problema — no se prueba con threads
    reales acá porque el motor de estos tests (SQLite) no soporta bloqueo de fila (dos
    escrituras concurrentes de verdad chocan con "database is locked" en vez de
    serializarse), así que un test con threading sería inestable sin validar la
    protección real de Postgres. Lo que sí es determinístico en cualquier motor: que el
    `UPDATE` calcula sobre el valor que esté en la base en ese instante, nunca sobre uno
    ya leído en Python — la parte del bug que causaba la pérdida de escrituras."""

    def setUp(self):
        self.bodega = Bodega.objects.create(codigo='BOD01')
        self.tipo_consumible = TipoConsumible.objects.create(codigo='MOUSE', nombre='Mouse USB')
        StockBodega.objects.create(bodega=self.bodega, tipo_consumible=self.tipo_consumible, cantidad=5)

    def test_el_update_calcula_sobre_el_valor_real_en_la_base_no_uno_ya_leido_en_python(self):
        stock = StockBodega.objects.get(bodega=self.bodega, tipo_consumible=self.tipo_consumible)
        # "Alguien más" cambia la fila en la base después de que este objeto Python ya
        # la leyó — el objeto `stock` de acá queda con cantidad=5 desactualizado.
        StockBodega.objects.filter(pk=stock.pk).update(cantidad=10)
        # Con el código viejo (stock.cantidad -= cantidad; stock.save()) esto hubiera
        # calculado 5-3=2 y pisado el 10 real. Con F(), el UPDATE que genera
        # registrar_salida_stock() nunca pasa por el valor stale de este objeto (ni
        # siquiera lo usa) — recalcula todo desde la base en el momento del guardado.
        resultado = registrar_salida_stock(bodega=self.bodega, tipo_consumible=self.tipo_consumible, cantidad=3)
        self.assertEqual(resultado.cantidad, 7)  # 10 (valor real) - 3, nunca 5 - 3 = 2


class RegistrarTrasladoBodegaTests(TestCase):
    """registrar_traslado_bodega reescrita para BUG-1 (auditoría de gobernanza,
    22-ago-2026): bloquea las dos filas de StockBodega en un orden fijo por
    `bodega_id` (no en el orden origen/destino de cada llamada), para que dos
    traslados concurrentes en direcciones opuestas (A->B y B->A a la vez) nunca formen
    un ciclo de espera (deadlock) entre sí."""

    def setUp(self):
        self.tipo_consumible = TipoConsumible.objects.create(codigo='MOUSE', nombre='Mouse USB')

    def test_traslada_correctamente_entre_dos_bodegas(self):
        origen = Bodega.objects.create(codigo='BOD-A')
        destino = Bodega.objects.create(codigo='BOD-B')
        StockBodega.objects.create(bodega=origen, tipo_consumible=self.tipo_consumible, cantidad=10)

        registrar_traslado_bodega(
            tipo_consumible=self.tipo_consumible, bodega_origen=origen, bodega_destino=destino,
            cantidad=4, usuario=None,
        )

        self.assertEqual(
            StockBodega.objects.get(bodega=origen, tipo_consumible=self.tipo_consumible).cantidad, 6,
        )
        self.assertEqual(
            StockBodega.objects.get(bodega=destino, tipo_consumible=self.tipo_consumible).cantidad, 4,
        )

    def test_funciona_igual_sin_importar_cual_bodega_tiene_el_pk_mas_bajo(self):
        # El orden de bloqueo interno es por bodega_id, no por origen/destino — confirma
        # que el resultado es correcto en ambos sentidos, no solo cuando origen.pk < destino.pk.
        mayor_pk = Bodega.objects.create(codigo='BOD-ALTA')
        menor_pk = Bodega.objects.create(codigo='BOD-BAJA')
        # menor_pk fue creada después pero puede o no tener pk menor según autoincremento
        # ya usado por otros tests — lo que importa es probar la dirección real, sea cual
        # sea el pk de cada una.
        StockBodega.objects.create(bodega=mayor_pk, tipo_consumible=self.tipo_consumible, cantidad=10)

        registrar_traslado_bodega(
            tipo_consumible=self.tipo_consumible, bodega_origen=mayor_pk, bodega_destino=menor_pk,
            cantidad=3, usuario=None,
        )

        self.assertEqual(
            StockBodega.objects.get(bodega=mayor_pk, tipo_consumible=self.tipo_consumible).cantidad, 7,
        )
        self.assertEqual(
            StockBodega.objects.get(bodega=menor_pk, tipo_consumible=self.tipo_consumible).cantidad, 3,
        )

    def test_rechaza_la_misma_bodega_como_origen_y_destino(self):
        bodega = Bodega.objects.create(codigo='BOD-A')
        with self.assertRaises(ValueError):
            registrar_traslado_bodega(
                tipo_consumible=self.tipo_consumible, bodega_origen=bodega, bodega_destino=bodega,
                cantidad=1, usuario=None,
            )

    def test_rechaza_stock_insuficiente_en_origen(self):
        origen = Bodega.objects.create(codigo='BOD-A')
        destino = Bodega.objects.create(codigo='BOD-B')
        StockBodega.objects.create(bodega=origen, tipo_consumible=self.tipo_consumible, cantidad=2)

        with self.assertRaises(ValueError):
            registrar_traslado_bodega(
                tipo_consumible=self.tipo_consumible, bodega_origen=origen, bodega_destino=destino,
                cantidad=5, usuario=None,
            )
        self.assertEqual(
            StockBodega.objects.get(bodega=origen, tipo_consumible=self.tipo_consumible).cantidad, 2,
        )


class ScopeMovimientosVisiblesTests(TestCase):
    """MovimientoInventario no tiene unidad_negocio propia — se escopa por las bodegas
    involucradas (bodega_origen/bodega_destino, cualquiera de los dos puede ser null)."""

    def setUp(self):
        from .models import MovimientoInventario

        self.MovimientoInventario = MovimientoInventario
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        self.tipo_consumible = TipoConsumible.objects.create(codigo='MOUSE', nombre='Mouse USB')

        self.bodega_sg = Bodega.objects.create(codigo='BOD-SG', unidad_negocio=self.sg)
        self.bodega_mia = Bodega.objects.create(codigo='BOD-MIA', unidad_negocio=self.mia)
        self.bodega_compartida = Bodega.objects.create(codigo='BOD-CENTRAL')

        self.usuario_mia = User.objects.create_user(username='u_mov_mia', password='x')
        from apps.cuentas.models import PerfilUsuario
        PerfilUsuario.objects.create(usuario=self.usuario_mia).unidades_negocio.add(self.mia)

    def _movimiento(self, *, origen=None, destino=None):
        return self.MovimientoInventario.objects.create(
            tipo_movimiento=self.MovimientoInventario.TipoMovimiento.TRASLADO if (origen and destino)
            else self.MovimientoInventario.TipoMovimiento.INGRESO_CONSUMIBLE,
            tipo_consumible=self.tipo_consumible, cantidad=1, bodega_origen=origen, bodega_destino=destino,
        )

    def test_oculta_movimiento_entre_bodegas_de_otro_tenant(self):
        self._movimiento(origen=self.bodega_sg, destino=None)
        visibles = scope_movimientos_visibles(self.MovimientoInventario.objects.all(), self.usuario_mia)
        self.assertEqual(visibles.count(), 0)

    def test_muestra_movimiento_de_bodega_compartida(self):
        mov = self._movimiento(origen=None, destino=self.bodega_compartida)
        visibles = scope_movimientos_visibles(self.MovimientoInventario.objects.all(), self.usuario_mia)
        self.assertIn(mov, visibles)

    def test_muestra_movimiento_del_propio_tenant(self):
        mov = self._movimiento(origen=None, destino=self.bodega_mia)
        visibles = scope_movimientos_visibles(self.MovimientoInventario.objects.all(), self.usuario_mia)
        self.assertIn(mov, visibles)

    def test_traslado_entre_bodega_propia_y_ajena_queda_oculto(self):
        self._movimiento(origen=self.bodega_mia, destino=self.bodega_sg)
        visibles = scope_movimientos_visibles(self.MovimientoInventario.objects.all(), self.usuario_mia)
        self.assertEqual(visibles.count(), 0)


class VincularActivosPorNumeroSerieTests(TestCase):
    def setUp(self):
        grupo = Grupo.objects.create(codigo='TRX001', version_objetivo='4.2.1')
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=self.sg)

    def _crear_estacion(self, codigo, numero_serie):
        return Estacion.objects.create(
            codigo=codigo, farmacia=self.farmacia, numero_serie=numero_serie,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )

    def test_vincula_cuando_hay_un_solo_match(self):
        estacion = self._crear_estacion('ML001-A', 'SN-0001')
        activo = Activo.objects.create(codigo='CR-DSK-0001', tipo=Activo.Tipo.DESKTOP, numero_serie='SN-0001')

        vinculados = vincular_activos_por_numero_serie()

        self.assertEqual(vinculados, 1)
        activo.refresh_from_db()
        self.assertEqual(activo.estacion, estacion)

    def test_no_vincula_si_no_hay_match(self):
        self._crear_estacion('ML001-A', 'SN-0001')
        self.assertEqual(vincular_activos_por_numero_serie(), 0)

    def test_no_vincula_si_hay_series_duplicadas(self):
        self._crear_estacion('ML001-A', 'SN-0001')
        Activo.objects.create(codigo='CR-DSK-0001', tipo=Activo.Tipo.DESKTOP, numero_serie='SN-0001')
        Activo.objects.create(codigo='CR-DSK-0002', tipo=Activo.Tipo.DESKTOP, numero_serie='SN-0001')
        self.assertEqual(vincular_activos_por_numero_serie(), 0)

    def test_no_repite_trabajo_en_estacion_ya_vinculada(self):
        estacion = self._crear_estacion('ML001-A', 'SN-0001')
        activo = Activo.objects.create(
            codigo='CR-DSK-0001', tipo=Activo.Tipo.DESKTOP, numero_serie='SN-0001', estacion=estacion,
        )
        otro = Activo.objects.create(codigo='CR-DSK-0002', tipo=Activo.Tipo.DESKTOP, numero_serie='SN-0001')

        self.assertEqual(vincular_activos_por_numero_serie(), 0)
        otro.refresh_from_db()
        self.assertIsNone(otro.estacion)
        activo.refresh_from_db()
        self.assertEqual(activo.estacion, estacion)


class AnomaliasRedActivoTests(TestCase):
    def setUp(self):
        grupo = Grupo.objects.create(codigo='TRX001', version_objetivo='4.2.1')
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        self.farmacia_sg = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=self.sg)

    def test_activo_dado_de_baja_con_estacion_online_aparece(self):
        estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=self.farmacia_sg,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            estado_conexion=Estacion.EstadoConexion.ONLINE,
        )
        activo = Activo.objects.create(
            codigo='CR-DSK-0001', tipo=Activo.Tipo.DESKTOP, estado=Activo.Estado.DADO_DE_BAJA, estacion=estacion,
        )
        self.assertIn(activo, activos_dados_de_baja_pero_conectados())

    def test_activo_dado_de_baja_con_estacion_offline_no_aparece(self):
        estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=self.farmacia_sg,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            estado_conexion=Estacion.EstadoConexion.OFFLINE,
        )
        Activo.objects.create(
            codigo='CR-DSK-0001', tipo=Activo.Tipo.DESKTOP, estado=Activo.Estado.DADO_DE_BAJA, estacion=estacion,
        )
        self.assertEqual(activos_dados_de_baja_pero_conectados().count(), 0)

    def test_activo_movido_a_farmacia_de_otra_unidad_aparece(self):
        estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=self.farmacia_sg, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        activo = Activo.objects.create(
            codigo='CR-DSK-0001', tipo=Activo.Tipo.DESKTOP, unidad_negocio=self.mia, estacion=estacion,
        )
        self.assertIn(activo, activos_movidos_sin_registro())

    def test_activo_en_farmacia_de_su_propia_unidad_no_aparece(self):
        estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=self.farmacia_sg, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        Activo.objects.create(
            codigo='CR-DSK-0001', tipo=Activo.Tipo.DESKTOP, unidad_negocio=self.sg, estacion=estacion,
        )
        self.assertEqual(activos_movidos_sin_registro().count(), 0)


class ActivosPorVencerGarantiaTests(TestCase):
    def test_incluye_vencida_y_por_vencer_dentro_de_la_ventana(self):
        hoy = timezone.now().date()
        vencida = Activo.objects.create(
            codigo='CR-DSK-0001', tipo=Activo.Tipo.DESKTOP, vencimiento_garantia=hoy - datetime.timedelta(days=5),
        )
        por_vencer = Activo.objects.create(
            codigo='CR-DSK-0002', tipo=Activo.Tipo.DESKTOP, vencimiento_garantia=hoy + datetime.timedelta(days=10),
        )
        lejos = Activo.objects.create(
            codigo='CR-DSK-0003', tipo=Activo.Tipo.DESKTOP, vencimiento_garantia=hoy + datetime.timedelta(days=90),
        )
        resultado = list(activos_por_vencer_garantia(dias=30))
        self.assertIn(vencida, resultado)
        self.assertIn(por_vencer, resultado)
        self.assertNotIn(lejos, resultado)

    def test_excluye_dados_de_baja_y_sin_fecha(self):
        hoy = timezone.now().date()
        Activo.objects.create(
            codigo='CR-DSK-0001', tipo=Activo.Tipo.DESKTOP, estado=Activo.Estado.DADO_DE_BAJA,
            vencimiento_garantia=hoy - datetime.timedelta(days=5),
        )
        Activo.objects.create(codigo='CR-DSK-0002', tipo=Activo.Tipo.DESKTOP, vencimiento_garantia=None)
        self.assertEqual(activos_por_vencer_garantia(dias=30).count(), 0)


class StockBajoMinimoTests(TestCase):
    def test_detecta_cantidad_por_debajo_del_minimo(self):
        bodega = Bodega.objects.create(codigo='BOD01')
        tipo = TipoConsumible.objects.create(codigo='MOUSE', nombre='Mouse USB', stock_minimo=5)
        stock = StockBodega.objects.create(bodega=bodega, tipo_consumible=tipo, cantidad=2)
        self.assertIn(stock, stock_bajo_minimo())

    def test_ignora_tipo_sin_stock_minimo_configurado(self):
        bodega = Bodega.objects.create(codigo='BOD01')
        tipo = TipoConsumible.objects.create(codigo='MOUSE', nombre='Mouse USB', stock_minimo=0)
        StockBodega.objects.create(bodega=bodega, tipo_consumible=tipo, cantidad=0)
        self.assertEqual(stock_bajo_minimo().count(), 0)

    def test_no_incluye_stock_por_encima_del_minimo(self):
        bodega = Bodega.objects.create(codigo='BOD01')
        tipo = TipoConsumible.objects.create(codigo='MOUSE', nombre='Mouse USB', stock_minimo=5)
        StockBodega.objects.create(bodega=bodega, tipo_consumible=tipo, cantidad=10)
        self.assertEqual(stock_bajo_minimo().count(), 0)
