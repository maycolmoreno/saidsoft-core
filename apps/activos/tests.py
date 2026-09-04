import datetime

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.catalogo.models import Estacion, Farmacia, Grupo, UnidadNegocio

from .models import (
    Activo, Bodega, Cargo, Colaborador, Departamento, EventoActivo, MovimientoInventario, OrdenCompra,
    OrdenCompraDetalle, RecepcionLote, StockBodega, TipoConsumible,
)
from .services import (
    activos_dados_de_baja_pero_conectados, activos_movidos_sin_registro, activos_por_vencer_garantia, anular_recepcion_lote, datos_hardware_desde_estacion, registrar_ajuste_inventario, registrar_asignacion, registrar_ingreso, registrar_recepcion_lote, registrar_salida_stock, registrar_traslado_bodega, registrar_ubicacion_farmacia, scope_movimientos_visibles, stock_bajo_minimo, vincular_activos_por_numero_serie,
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

    def test_soporte_tecnico_puede_dar_de_alta_un_equipo(self):
        """Sin `add_activo`, la app tenia "Registrar equipo" pero nadie en campo podia
        usarlo — y como no se abre un mantenimiento sin elegir equipo, un tecnico frente
        a una maquina no inventariada quedaba sin salida: no podia cargarla ni atenderla."""
        call_command('seed_permisos')
        codenames = set(
            Group.objects.get(name='Soporte Técnico')
            .permissions.filter(content_type__app_label='activos')
            .values_list('codename', flat=True)
        )
        self.assertIn('add_activo', codenames)
        self.assertIn('view_activo', codenames)
        self.assertIn('change_activo', codenames)

    def test_solicitante_de_viaticos_carga_pero_no_aprueba(self):
        """El técnico ("Solicitante" de GFI-GTC-PR002) reporta su gasto y ve el suyo.
        `change_reporteviatico` es lo que habilita aprobar: dárselo sería dejar que
        apruebe su propio viático, que es justo lo que el módulo viene a impedir."""
        call_command('seed_permisos')
        codenames = set(
            Group.objects.get(name='Soporte Técnico')
            .permissions.filter(content_type__app_label='viaticos')
            .values_list('codename', flat=True)
        )
        self.assertEqual(codenames, {'view_reporteviatico', 'add_reporteviatico'})

    def test_coordinador_de_viaticos_aprueba_pero_no_carga(self):
        """Quien aprueba no es quien gasta: el coordinador no tiene `add`."""
        call_command('seed_permisos')
        codenames = set(
            Group.objects.get(name='Coordinador de Viáticos')
            .permissions.filter(content_type__app_label='viaticos')
            .values_list('codename', flat=True)
        )
        self.assertIn('change_reporteviatico', codenames)
        self.assertIn('view_colaboradorzona', codenames)
        self.assertNotIn('add_reporteviatico', codenames)

    def test_soporte_tecnico_tiene_inventario_como_el_rol_tecnico(self):
        # Confirmado con el usuario (22-ago-2026): el técnico de campo registra en
        # SAIDSOFT los equipos que reemplaza/mueve en una farmacia, mismo set que el
        # rol 'Técnico' heredado de InvTICS.
        call_command('seed_permisos')
        grupo = Group.objects.get(name='Soporte Técnico')
        codenames = set(grupo.permissions.values_list('codename', flat=True))
        self.assertTrue({
            'view_activo', 'change_activo', 'view_eventoactivo', 'add_eventoactivo',
            'view_ubicacion', 'view_colaborador',
        }.issubset(codenames))


class CrearTecnicosSoporteTests(TestCase):
    """Alta de los 9 técnicos de soporte de campo (Colaborador + login real), datos
    reales de RRHH recibidos por chat el 22-ago-2026."""

    def setUp(self):
        call_command('seed_permisos')
        import tempfile
        self.archivo_passwords = tempfile.NamedTemporaryFile(suffix='.txt', delete=False).name

    def _correr(self):
        call_command('crear_tecnicos_soporte', archivo_passwords=self.archivo_passwords)

    def test_crea_los_9_colaboradores_con_login_y_grupo_correcto(self):
        self._correr()
        self.assertEqual(Colaborador.objects.count(), 9)
        grupo_soporte = Group.objects.get(name='Soporte Técnico')

        jaime = Colaborador.objects.get(cedula='1312655291')
        self.assertEqual(jaime.nombre, 'Carranza Cedeño Jaime Leonerys')
        self.assertIsNotNone(jaime.usuario_id)
        self.assertEqual(jaime.usuario.username, 'jaime.carranza')
        self.assertIn(grupo_soporte, jaime.usuario.groups.all())
        self.assertFalse(jaime.usuario.is_superuser)  # nunca Admin, aunque RRHH los marque así
        self.assertEqual(jaime.cargo.nombre, 'Asistente de Soporte Técnico')
        self.assertTrue(jaime.origen_sync)
        # Un login recién creado debe quedar con contraseña usable de verdad, no en
        # blanco -- de lo contrario nadie puede entrar aunque el registro exista.
        self.assertTrue(jaime.usuario.has_usable_password())

    def test_escribe_las_contrasenas_generadas_en_el_archivo_no_en_pantalla(self):
        import io
        buffer_salida = io.StringIO()
        call_command('crear_tecnicos_soporte', archivo_passwords=self.archivo_passwords, stdout=buffer_salida)
        salida = buffer_salida.getvalue()
        with open(self.archivo_passwords, encoding='utf-8') as f:
            contenido = f.read()
        self.assertIn('jaime.carranza,', contenido)
        self.assertEqual(contenido.count('\n'), 10)  # encabezado + 9 técnicos
        # La contraseña real no debe aparecer en la salida por consola del comando.
        lineas_passwords = contenido.strip().splitlines()[1:]
        for linea in lineas_passwords:
            _, password = linea.split(',')
            self.assertNotIn(password, salida)

    def test_correr_dos_veces_no_duplica_ni_regenera_contrasenas(self):
        self._correr()

        otro_archivo = self.archivo_passwords + '.2'
        call_command('crear_tecnicos_soporte', archivo_passwords=otro_archivo)

        self.assertEqual(Colaborador.objects.count(), 9)
        self.assertEqual(User.objects.count(), 9)
        # Nadie necesitaba login nuevo la segunda vez -- ni se crea el archivo.
        import os
        self.assertFalse(os.path.exists(otro_archivo))

    def test_sin_el_grupo_soporte_tecnico_falla_con_mensaje_claro(self):
        Group.objects.filter(name='Soporte Técnico').delete()
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            self._correr()

    def test_dry_run_no_escribe_nada(self):
        call_command('crear_tecnicos_soporte', archivo_passwords=self.archivo_passwords, dry_run=True)
        self.assertEqual(Colaborador.objects.count(), 0)
        self.assertEqual(User.objects.count(), 0)
        # setUp ya crea el archivo vacío (tempfile.NamedTemporaryFile) -- en modo
        # prueba debe seguir vacío, nunca escribirse con contraseñas de verdad.
        with open(self.archivo_passwords, encoding='utf-8') as f:
            self.assertEqual(f.read(), '')

    def test_login_nuevo_recibe_perfil_con_acceso_a_todas_las_unidades(self):
        # Sin PerfilUsuario, apps.cuentas.services.unidades_negocio_visibles() devuelve
        # queryset vacío -- el técnico entraría y no vería ninguna farmacia/estación,
        # aunque tenga todos los permisos de Django del grupo Soporte Técnico.
        self._correr()
        jaime = Colaborador.objects.get(cedula='1312655291')
        self.assertTrue(jaime.usuario.perfil.acceso_todas_unidades)

    def test_login_ya_existente_tambien_recibe_el_perfil(self):
        from apps.cuentas.models import PerfilUsuario
        from apps.cuentas.services import unidades_negocio_visibles

        usuario = User.objects.create_user(username='jaime.carranza', password='x')
        Colaborador.objects.create(
            cedula='1312655291', nombre='Carranza Cedeño Jaime Leonerys',
            correo='jaime.carranza@cresio.com', usuario=usuario,
        )
        self.assertFalse(PerfilUsuario.objects.filter(usuario=usuario).exists())

        self._correr()

        usuario.refresh_from_db()
        self.assertTrue(usuario.perfil.acceso_todas_unidades)
        self.assertEqual(unidades_negocio_visibles(usuario).count(), UnidadNegocio.objects.count())

    def test_supervisores_regionales_reciben_permiso_individual_de_aprobar(self):
        # Confirmado con el usuario (22-ago-2026): solo los 2 supervisores regionales
        # (Luis Figueroa, Diego Aguilar) aprueban lo que crean sus asistentes -- permiso
        # INDIVIDUAL, nunca de grupo (así quedó decidido al cerrar AC-3/AC-2).
        self._correr()
        luis = Colaborador.objects.get(cedula='1310909906')
        diego = Colaborador.objects.get(cedula='0706884947')
        for colaborador in (luis, diego):
            self.assertTrue(colaborador.usuario.has_perm('scripts.aprobar_ejecucionscript'))
            self.assertTrue(colaborador.usuario.has_perm('despliegues.aprobar_despliegue'))

    def test_asistentes_no_reciben_el_permiso_de_aprobar(self):
        self._correr()
        jaime = Colaborador.objects.get(cedula='1312655291')  # Asistente, no supervisor
        self.assertFalse(jaime.usuario.has_perm('scripts.aprobar_ejecucionscript'))
        self.assertFalse(jaime.usuario.has_perm('despliegues.aprobar_despliegue'))

    def test_supervisor_con_login_ya_existente_tambien_recibe_el_permiso(self):
        usuario = User.objects.create_user(username='luis.figueroa', password='x')
        Colaborador.objects.create(
            cedula='1310909906', nombre='Figueroa Parraga Luis Miguel',
            correo='luis.figueroa@cresio.com', usuario=usuario,
        )
        self._correr()
        usuario.refresh_from_db()
        self.assertTrue(usuario.has_perm('scripts.aprobar_ejecucionscript'))
        self.assertTrue(usuario.has_perm('despliegues.aprobar_despliegue'))


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


class RegistrarAjusteInventarioTests(TestCase):
    """BUG-3 de la auditoría de gobernanza (22-ago-2026):
    MovimientoInventario.TipoMovimiento.AJUSTE existía en las choices desde siempre
    pero nada lo generaba nunca."""

    def setUp(self):
        self.tipo_consumible = TipoConsumible.objects.create(codigo='MOUSE', nombre='Mouse USB')
        self.bodega = Bodega.objects.create(codigo='BOD-A')
        StockBodega.objects.create(bodega=self.bodega, tipo_consumible=self.tipo_consumible, cantidad=10)

    def test_ajuste_positivo_suma_stock_y_deja_movimiento_en_bodega_destino(self):
        movimiento = registrar_ajuste_inventario(
            bodega=self.bodega, tipo_consumible=self.tipo_consumible, cantidad_delta=3,
            motivo='Conteo físico: sobraron 3 unidades', usuario=None,
        )
        self.assertEqual(
            StockBodega.objects.get(bodega=self.bodega, tipo_consumible=self.tipo_consumible).cantidad, 13,
        )
        self.assertEqual(movimiento.tipo_movimiento, MovimientoInventario.TipoMovimiento.AJUSTE)
        self.assertEqual(movimiento.bodega_destino, self.bodega)
        self.assertIsNone(movimiento.bodega_origen)

    def test_ajuste_negativo_resta_stock_y_deja_movimiento_en_bodega_origen(self):
        registrar_ajuste_inventario(
            bodega=self.bodega, tipo_consumible=self.tipo_consumible, cantidad_delta=-4,
            motivo='Merma detectada en conteo físico', usuario=None,
        )
        self.assertEqual(
            StockBodega.objects.get(bodega=self.bodega, tipo_consumible=self.tipo_consumible).cantidad, 6,
        )
        movimiento = MovimientoInventario.objects.get()
        self.assertEqual(movimiento.bodega_origen, self.bodega)
        self.assertIsNone(movimiento.bodega_destino)

    def test_rechaza_cantidad_cero(self):
        with self.assertRaises(ValueError):
            registrar_ajuste_inventario(
                bodega=self.bodega, tipo_consumible=self.tipo_consumible, cantidad_delta=0,
                motivo='algo', usuario=None,
            )

    def test_rechaza_sin_motivo(self):
        with self.assertRaises(ValueError):
            registrar_ajuste_inventario(
                bodega=self.bodega, tipo_consumible=self.tipo_consumible, cantidad_delta=1,
                motivo='   ', usuario=None,
            )

    def test_rechaza_si_deja_el_stock_en_negativo(self):
        with self.assertRaises(ValueError):
            registrar_ajuste_inventario(
                bodega=self.bodega, tipo_consumible=self.tipo_consumible, cantidad_delta=-20,
                motivo='merma grande', usuario=None,
            )
        self.assertEqual(
            StockBodega.objects.get(bodega=self.bodega, tipo_consumible=self.tipo_consumible).cantidad, 10,
        )


class AnularRecepcionLoteTests(TestCase):
    """BUG-3: RecepcionLote.Estado.ANULADO existía en las choices desde siempre pero
    nada lo asignaba nunca — no había forma de revertir una recepción mal cargada."""

    def setUp(self):
        self.tipo_consumible = TipoConsumible.objects.create(codigo='MOUSE', nombre='Mouse USB')
        self.bodega = Bodega.objects.create(codigo='BOD-A')
        self.oc = OrdenCompra.objects.create(numero_oc='OC-0001', proveedor='ACME', fecha_emision='2026-01-01')
        self.detalle = OrdenCompraDetalle.objects.create(
            orden_compra=self.oc, tipo_item=OrdenCompraDetalle.TipoItem.CONSUMIBLE,
            tipo_consumible=self.tipo_consumible, cantidad_solicitada=10,
        )

    def test_anular_revierte_stock_y_cantidad_recibida(self):
        recepcion = registrar_recepcion_lote(detalle=self.detalle, cantidad=6, bodega=self.bodega, usuario=None)
        self.assertEqual(
            StockBodega.objects.get(bodega=self.bodega, tipo_consumible=self.tipo_consumible).cantidad, 6,
        )
        self.detalle.refresh_from_db()
        self.assertEqual(self.detalle.cantidad_recibida, 6)
        self.assertEqual(self.detalle.estado, OrdenCompraDetalle.Estado.PARCIAL)

        anular_recepcion_lote(recepcion=recepcion, usuario=None, motivo='Cantidad mal cargada')

        self.assertEqual(
            StockBodega.objects.get(bodega=self.bodega, tipo_consumible=self.tipo_consumible).cantidad, 0,
        )
        self.detalle.refresh_from_db()
        self.assertEqual(self.detalle.cantidad_recibida, 0)
        self.assertEqual(self.detalle.estado, OrdenCompraDetalle.Estado.PENDIENTE)
        recepcion.refresh_from_db()
        self.assertEqual(recepcion.estado, RecepcionLote.Estado.ANULADO)
        movimiento_reverso = MovimientoInventario.objects.get(recepcion_lote=recepcion, tipo_movimiento=MovimientoInventario.TipoMovimiento.AJUSTE)
        self.assertEqual(movimiento_reverso.cantidad, -6)

    def test_anular_una_de_dos_recepciones_deja_la_linea_en_parcial(self):
        recepcion_1 = registrar_recepcion_lote(detalle=self.detalle, cantidad=4, bodega=self.bodega, usuario=None)
        self.detalle.refresh_from_db()
        registrar_recepcion_lote(detalle=self.detalle, cantidad=3, bodega=self.bodega, usuario=None)

        anular_recepcion_lote(recepcion=recepcion_1, usuario=None)

        self.detalle.refresh_from_db()
        self.assertEqual(self.detalle.cantidad_recibida, 3)
        self.assertEqual(self.detalle.estado, OrdenCompraDetalle.Estado.PARCIAL)
        self.assertEqual(
            StockBodega.objects.get(bodega=self.bodega, tipo_consumible=self.tipo_consumible).cantidad, 3,
        )

    def test_no_se_puede_anular_dos_veces(self):
        recepcion = registrar_recepcion_lote(detalle=self.detalle, cantidad=6, bodega=self.bodega, usuario=None)
        anular_recepcion_lote(recepcion=recepcion, usuario=None)
        with self.assertRaises(ValueError):
            anular_recepcion_lote(recepcion=recepcion, usuario=None)

    def test_no_se_puede_anular_si_el_stock_ya_se_uso(self):
        recepcion = registrar_recepcion_lote(detalle=self.detalle, cantidad=6, bodega=self.bodega, usuario=None)
        # Se consume parte de ese stock antes de intentar anular la recepción.
        registrar_salida_stock(bodega=self.bodega, tipo_consumible=self.tipo_consumible, cantidad=4)

        with self.assertRaises(ValueError):
            anular_recepcion_lote(recepcion=recepcion, usuario=None)
        recepcion.refresh_from_db()
        self.assertNotEqual(recepcion.estado, RecepcionLote.Estado.ANULADO)


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

    def test_vincular_sincroniza_la_farmacia_desde_la_estacion(self):
        # Antes solo se sincronizaba `estacion` -- un activo vinculado por RMM se
        # quedaba sin forma de saber en qué farmacia está si nadie lo cargaba a mano
        # (23-ago-2026).
        self._crear_estacion('ML001-A', 'SN-0001')
        activo = Activo.objects.create(codigo='CR-DSK-0001', tipo=Activo.Tipo.DESKTOP, numero_serie='SN-0001')

        vincular_activos_por_numero_serie()

        activo.refresh_from_db()
        self.assertEqual(activo.farmacia, self.farmacia)

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


class RegistrarUbicacionFarmaciaTests(TestCase):
    """Activo.farmacia: cómo diferenciar un equipo de farmacia de uno administrativo
    para equipos sin agente RMM (impresoras, monitores, PCs sin agente) -- 23-ago-2026."""

    def setUp(self):
        self.usuario = User.objects.create_user(username='u_farmacia', password='x')
        grupo = Grupo.objects.create(codigo='TRX001', version_objetivo='4.2.1')
        sg = UnidadNegocio.objects.get(codigo='SG')
        self.farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg)
        self.otra_farmacia = Farmacia.objects.create(codigo='ML002', grupo=grupo, unidad_negocio=sg)
        self.activo = Activo.objects.create(codigo='CR-IMP-0001', tipo=Activo.Tipo.IMPRESORA)

    def test_asigna_farmacia_y_registra_evento(self):
        registrar_ubicacion_farmacia(activo=self.activo, farmacia=self.farmacia, usuario=self.usuario)
        self.activo.refresh_from_db()
        self.assertEqual(self.activo.farmacia, self.farmacia)
        self.assertTrue(
            EventoActivo.objects.filter(
                activo=self.activo, tipo_evento=EventoActivo.TipoEvento.UBICACION_ACTUALIZADA,
            ).exists(),
        )

    def test_limpiar_farmacia_lo_vuelve_administrativo(self):
        self.activo.farmacia = self.farmacia
        self.activo.save(update_fields=['farmacia'])
        registrar_ubicacion_farmacia(activo=self.activo, farmacia=None, usuario=self.usuario)
        self.activo.refresh_from_db()
        self.assertIsNone(self.activo.farmacia)

    def test_rechaza_activo_dado_de_baja(self):
        self.activo.estado = Activo.Estado.DADO_DE_BAJA
        self.activo.save(update_fields=['estado'])
        with self.assertRaises(ValueError):
            registrar_ubicacion_farmacia(activo=self.activo, farmacia=self.farmacia, usuario=self.usuario)

    def test_rechaza_activo_con_estacion_rmm_vinculada(self):
        estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=self.farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self.activo.estacion = estacion
        self.activo.save(update_fields=['estacion'])
        with self.assertRaises(ValueError):
            registrar_ubicacion_farmacia(activo=self.activo, farmacia=self.otra_farmacia, usuario=self.usuario)


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


class DatosHardwareDesdeEstacionTests(TestCase):
    """Precarga del alta de activo desde lo que el agente RMM ya reporta.

    Evita reingresar a mano lo que la estación sabe, y que inventario y monitoreo
    terminen diciendo cosas distintas del mismo equipo.
    """

    def setUp(self):
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg)
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, numero_serie='MXL8192898',
            procesador='11th Gen Intel(R) Core(TM) i3-1115G4', ram_total_mb=7839,
            almacenamiento_total_gb=446,
        )

    def test_convierte_la_ram_de_mb_a_gb(self):
        # La estación reporta MB y el activo se lleva en GB: copiar el número tal cual
        # metería "7839" en un campo de gigabytes.
        datos = datos_hardware_desde_estacion('MXL8192898')
        self.assertEqual(datos['ram_gb'], 8)
        self.assertEqual(datos['almacenamiento_gb'], 446)
        self.assertEqual(datos['procesador'], '11th Gen Intel(R) Core(TM) i3-1115G4')
        self.assertEqual(datos['estacion'], self.estacion)

    def test_la_busqueda_ignora_mayusculas_y_espacios(self):
        self.assertIsNotNone(datos_hardware_desde_estacion('  mxl8192898  '))

    def test_sin_serie_devuelve_none(self):
        self.assertIsNone(datos_hardware_desde_estacion(''))
        self.assertIsNone(datos_hardware_desde_estacion('   '))

    def test_serie_desconocida_devuelve_none(self):
        self.assertIsNone(datos_hardware_desde_estacion('NO-EXISTE'))

    def test_con_series_duplicadas_no_adivina(self):
        # Dato sucio: dos estaciones con la misma serie. Devolver una al azar sería
        # peor que no completar nada.
        otra_farmacia = Farmacia.objects.create(
            codigo='ML002', grupo=Grupo.objects.get(codigo='TRX001'),
            unidad_negocio=UnidadNegocio.objects.get(codigo='SG'),
        )
        Estacion.objects.create(
            codigo='ML002-A', farmacia=otra_farmacia, numero_serie='MXL8192898',
        )
        self.assertIsNone(datos_hardware_desde_estacion('MXL8192898'))

    def test_solo_devuelve_los_campos_que_la_estacion_tiene(self):
        # Sin RAM ni disco cargados no se manda la clave, para no pisar con vacíos lo
        # que el usuario ya haya escrito.
        self.estacion.ram_total_mb = None
        self.estacion.almacenamiento_total_gb = None
        self.estacion.save(update_fields=['ram_total_mb', 'almacenamiento_total_gb'])
        datos = datos_hardware_desde_estacion('MXL8192898')
        self.assertIn('procesador', datos)
        self.assertNotIn('ram_gb', datos)
        self.assertNotIn('almacenamiento_gb', datos)


class RegistrarIngresoSinBodegaTests(TestCase):
    """Inventariar un equipo que YA está instalado en una farmacia.

    El flujo original exigía una bodega, lo que obligaba a inventar uno por el que el
    equipo nunca pasó.
    """

    def setUp(self):
        self.usuario = User.objects.create_user(username='u_ingreso', password='x')
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia = Farmacia.objects.create(codigo='MAM06', grupo=grupo, unidad_negocio=sg)
        self.bodega = Bodega.objects.create(codigo='BOD-1', nombre='Central')

    def _ingresar(self, **extra):
        return registrar_ingreso(
            tipo=Activo.Tipo.DESKTOP, marca=None, modelo='OptiPlex', numero_serie='BB3VJD3',
            fecha_compra=None, vencimiento_garantia=None, orden_compra=None,
            usuario=self.usuario, **extra,
        )

    def test_con_farmacia_queda_en_servicio_y_sin_bodega(self):
        activo = self._ingresar(farmacia=self.farmacia)
        self.assertEqual(activo.farmacia, self.farmacia)
        self.assertIsNone(activo.bodega_actual)
        self.assertEqual(activo.estado, Activo.Estado.ASIGNADO)
        # Un equipo que ya está operando no es "nuevo".
        self.assertEqual(activo.estado_fisico_actual, Activo.EstadoFisico.BUENO)

    def test_con_bodega_se_comporta_como_siempre(self):
        activo = self._ingresar(bodega=self.bodega)
        self.assertEqual(activo.bodega_actual, self.bodega)
        self.assertIsNone(activo.farmacia)
        self.assertEqual(activo.estado, Activo.Estado.EN_BODEGA)
        self.assertEqual(activo.estado_fisico_actual, Activo.EstadoFisico.NUEVO)

    def test_sin_bodega_ni_farmacia_no_se_puede(self):
        # Un activo tiene que estar en algún lado.
        with self.assertRaises(ValueError):
            self._ingresar()

    def test_el_evento_deja_constancia_de_donde_entro(self):
        activo = self._ingresar(farmacia=self.farmacia)
        evento = activo.eventos.get(tipo_evento=EventoActivo.TipoEvento.INGRESO)
        self.assertEqual(evento.detalle['farmacia'], 'MAM06')
        self.assertIsNone(evento.detalle['bodega'])

    def test_se_puede_forzar_el_estado_fisico(self):
        activo = self._ingresar(
            farmacia=self.farmacia, estado_fisico=Activo.EstadoFisico.REGULAR,
        )
        self.assertEqual(activo.estado_fisico_actual, Activo.EstadoFisico.REGULAR)
