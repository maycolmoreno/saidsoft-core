from datetime import timedelta

from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.catalogo.models import Estacion, Farmacia, Grupo, UnidadNegocio

from .models import EjecucionScript, Script, ScriptProgramado, TipoScript
from .services import aprobar_ejecucion_script, generar_ejecucion_programada, registrar_ejecucion_script


class GenerarEjecucionProgramadaTests(TestCase):
    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=self.sg)
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self.usuario = User.objects.create_user(username='u', password='x')
        self.script = Script.objects.create(
            nombre='Actualizar winget', tipo=TipoScript.POWERSHELL, contenido='winget upgrade --all',
            creado_por=self.usuario,
        )
        self.programado = ScriptProgramado.objects.create(
            script=self.script, unidad_negocio=self.sg, destino_tipo=EjecucionScript.DestinoTipo.CADENA,
            frecuencia_dias=7, fecha_proxima_ejecucion=timezone.localdate(), creado_por=self.usuario,
        )

    def test_genera_ejecucion_y_avanza_fechas(self):
        hoy = timezone.localdate()
        ejecucion = generar_ejecucion_programada(programado=self.programado)

        self.assertEqual(ejecucion.script, self.script)
        self.assertEqual(ejecucion.programado, self.programado)
        self.assertEqual(ejecucion.resultados.count(), 1)  # la única estación aprobada de la cadena

        self.programado.refresh_from_db()
        self.assertEqual(self.programado.fecha_ultima_ejecucion, hoy)
        self.assertEqual(self.programado.fecha_proxima_ejecucion, hoy + timedelta(days=7))

    def test_comando_solo_recoge_las_vencidas(self):
        futuro = ScriptProgramado.objects.create(
            script=self.script, unidad_negocio=self.sg, destino_tipo=EjecucionScript.DestinoTipo.CADENA,
            frecuencia_dias=7, fecha_proxima_ejecucion=timezone.localdate() + timedelta(days=5),
            creado_por=self.usuario,
        )
        call_command('generar_ejecuciones_programadas')

        self.assertEqual(EjecucionScript.objects.filter(programado=self.programado).count(), 1)
        self.assertEqual(EjecucionScript.objects.filter(programado=futuro).count(), 0)

    def test_comando_no_recoge_inactivas(self):
        self.programado.activo = False
        self.programado.save(update_fields=['activo'])
        call_command('generar_ejecuciones_programadas')
        self.assertFalse(EjecucionScript.objects.filter(programado=self.programado).exists())


class RegistrarEjecucionScriptAprobacionTests(TestCase):
    """AC-3: destinos amplios (cadena/grupos/farmacias) quedan pendientes de aprobación
    y no se publican hasta que un segundo usuario las apruebe; destinos angostos
    (estaciones puntuales) y las ejecuciones generadas por ScriptProgramado siguen
    publicándose de inmediato, igual que antes de esta regla."""

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=self.sg)
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self.grupo = grupo
        self.creador = User.objects.create_user(username='creador', password='x')
        self.aprobador = User.objects.create_user(username='aprobador', password='x')
        self.script = Script.objects.create(
            nombre='Actualizar winget', tipo=TipoScript.POWERSHELL, contenido='winget upgrade --all',
            creado_por=self.creador,
        )

    def test_destino_cadena_queda_pendiente_de_aprobacion_sin_publicar(self):
        ejecucion = registrar_ejecucion_script(
            script=self.script, destino_tipo=EjecucionScript.DestinoTipo.CADENA,
            unidad_negocio=self.sg, usuario=self.creador,
        )
        self.assertEqual(ejecucion.estado, EjecucionScript.Estado.PENDIENTE_APROBACION)
        self.assertEqual(ejecucion.resultados.count(), 0)

    def test_destino_estaciones_no_requiere_aprobacion(self):
        ejecucion = registrar_ejecucion_script(
            script=self.script, destino_tipo=EjecucionScript.DestinoTipo.ESTACIONES,
            unidad_negocio=self.sg, usuario=self.creador, estaciones=[self.estacion],
        )
        self.assertNotEqual(ejecucion.estado, EjecucionScript.Estado.PENDIENTE_APROBACION)
        self.assertEqual(ejecucion.resultados.count(), 1)

    def test_ejecucion_programada_no_requiere_aprobacion_aunque_el_destino_sea_amplio(self):
        programado = ScriptProgramado.objects.create(
            script=self.script, unidad_negocio=self.sg, destino_tipo=EjecucionScript.DestinoTipo.CADENA,
            frecuencia_dias=7, fecha_proxima_ejecucion=timezone.localdate(), creado_por=self.creador,
        )
        ejecucion = generar_ejecucion_programada(programado=programado)
        self.assertNotEqual(ejecucion.estado, EjecucionScript.Estado.PENDIENTE_APROBACION)
        self.assertEqual(ejecucion.resultados.count(), 1)

    def test_omitir_aprobacion_publica_de_inmediato(self):
        ejecucion = registrar_ejecucion_script(
            script=self.script, destino_tipo=EjecucionScript.DestinoTipo.GRUPOS,
            unidad_negocio=self.sg, usuario=self.creador, grupos=[self.grupo], omitir_aprobacion=True,
        )
        self.assertNotEqual(ejecucion.estado, EjecucionScript.Estado.PENDIENTE_APROBACION)
        self.assertEqual(ejecucion.resultados.count(), 1)

    def test_aprobar_publica_la_ejecucion(self):
        ejecucion = registrar_ejecucion_script(
            script=self.script, destino_tipo=EjecucionScript.DestinoTipo.CADENA,
            unidad_negocio=self.sg, usuario=self.creador,
        )
        aprobar_ejecucion_script(ejecucion=ejecucion, usuario=self.aprobador)
        ejecucion.refresh_from_db()
        self.assertEqual(ejecucion.aprobado_por, self.aprobador)
        self.assertNotEqual(ejecucion.estado, EjecucionScript.Estado.PENDIENTE_APROBACION)
        self.assertEqual(ejecucion.resultados.count(), 1)

    def test_creador_no_puede_aprobar_su_propia_ejecucion(self):
        ejecucion = registrar_ejecucion_script(
            script=self.script, destino_tipo=EjecucionScript.DestinoTipo.CADENA,
            unidad_negocio=self.sg, usuario=self.creador,
        )
        with self.assertRaises(ValueError):
            aprobar_ejecucion_script(ejecucion=ejecucion, usuario=self.creador)
        ejecucion.refresh_from_db()
        self.assertEqual(ejecucion.estado, EjecucionScript.Estado.PENDIENTE_APROBACION)
        self.assertEqual(ejecucion.resultados.count(), 0)

    def test_no_se_puede_aprobar_dos_veces(self):
        ejecucion = registrar_ejecucion_script(
            script=self.script, destino_tipo=EjecucionScript.DestinoTipo.CADENA,
            unidad_negocio=self.sg, usuario=self.creador,
        )
        aprobar_ejecucion_script(ejecucion=ejecucion, usuario=self.aprobador)
        with self.assertRaises(ValueError):
            aprobar_ejecucion_script(ejecucion=ejecucion, usuario=self.aprobador)


# `CELERY_TASK_ALWAYS_EAGER` solo está puesto en config/settings/desarrollo.py, y no
# se lee del entorno. Sin este override la prueba pasa en local y falla SIEMPRE dentro
# del contenedor — que es justo donde CLAUDE.md pide correr la suite para probar contra
# PostgreSQL. Una prueba no debe depender de qué módulo de settings esté cargado.
@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
class GenerarEjecucionesProgramadasTaskTests(TestCase):
    """CELERY_TASK_ALWAYS_EAGER=True hace que .delay() corra sincrónico en el test."""

    def test_delay_genera_la_ejecucion_vencida(self):
        from apps.scripts.tasks import generar_ejecuciones_programadas_task

        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg)
        Estacion.objects.create(codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA)
        usuario = User.objects.create_user(username='u_task', password='x')
        script = Script.objects.create(
            nombre='Winget upgrade', tipo=TipoScript.POWERSHELL, contenido='winget upgrade --all',
            creado_por=usuario,
        )
        programado = ScriptProgramado.objects.create(
            script=script, unidad_negocio=sg, destino_tipo=EjecucionScript.DestinoTipo.CADENA,
            frecuencia_dias=7, fecha_proxima_ejecucion=timezone.localdate(), creado_por=usuario,
        )

        resultado = generar_ejecuciones_programadas_task.delay()

        self.assertTrue(EjecucionScript.objects.filter(programado=programado).exists())
        self.assertIn('1 ejecución', resultado.get())


class CambiarNodoPosTests(TestCase):
    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.grupo = Grupo.objects.create(codigo='TRX002')
        farmacia = Farmacia.objects.create(codigo='ML002', grupo=self.grupo, unidad_negocio=self.sg)
        self.estacion = Estacion.objects.create(
            codigo='ML002-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self.usuario = User.objects.create_user(username='operador', password='x')

    def _correr(self, **overrides):
        opciones = {
            'unidad_negocio': 'SG', 'grupo': 'TRX002', 'nodo': 'trx002', 'usuario': 'operador',
        }
        opciones.update(overrides)
        call_command('cambiar_nodo_pos', **opciones)

    def test_nodo_invalido_rechaza_sin_tocar_la_base(self):
        with self.assertRaises(CommandError):
            self._correr(nodo='trx002"; Remove-Item C:\\ -Recurse')
        self.assertFalse(Script.objects.exists())

    def test_unidad_negocio_inexistente(self):
        with self.assertRaises(CommandError):
            self._correr(unidad_negocio='NOEXISTE')

    def test_grupo_inexistente(self):
        with self.assertRaises(CommandError):
            self._correr(grupo='NOEXISTE')

    def test_usuario_inexistente(self):
        with self.assertRaises(CommandError):
            self._correr(usuario='noexiste')

    def test_arma_y_envia_en_una_sola_pasada(self):
        self._correr()

        script = Script.objects.get(es_adhoc=True)
        self.assertIn('trx002', script.contenido)
        self.assertIn("SelectSingleNode(\"//add[@key='Bdd']\")", script.contenido)
        self.assertEqual(script.unidad_negocio, self.sg)

        ejecucion = EjecucionScript.objects.get(script=script)
        self.assertEqual(ejecucion.destino_tipo, EjecucionScript.DestinoTipo.GRUPOS)
        self.assertEqual(list(ejecucion.grupos.all()), [self.grupo])
        self.assertEqual(ejecucion.resultados.count(), 1)
        self.assertEqual(ejecucion.resultados.first().estacion, self.estacion)

    def test_no_llega_a_estacion_de_otro_grupo(self):
        otro_grupo = Grupo.objects.create(codigo='TRX003')
        otra_farmacia = Farmacia.objects.create(codigo='ML003', grupo=otro_grupo, unidad_negocio=self.sg)
        Estacion.objects.create(
            codigo='ML003-A', farmacia=otra_farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self._correr()
        ejecucion = EjecucionScript.objects.get()
        self.assertEqual(ejecucion.resultados.count(), 1)
        self.assertEqual(ejecucion.resultados.first().estacion.codigo, 'ML002-A')


class SeedScriptsHoraTests(TestCase):
    """Scripts de biblioteca para diagnosticar y corregir la hora de una estación.

    El reloj corrido no es cosmético: pasado el desfase de la ventana de firma, la
    estación descarta en silencio todos los scripts y comandos del panel, incluido el que
    le arreglaría el reloj. Estos scripts son el camino para arreglarlo mientras todavía
    está dentro de la ventana.
    """

    def setUp(self):
        self.admin = User.objects.create_superuser(username='u_hora', password='x' * 14)

    def test_crea_los_dos_scripts_como_compartidos(self):
        """`unidad_negocio=None` los hace visibles para todos los clientes: un reloj
        corrido no es un problema de una unidad de negocio en particular."""
        call_command('seed_scripts_hora')
        scripts = Script.objects.filter(categoria='Hora')
        self.assertEqual(scripts.count(), 2)
        for s in scripts:
            self.assertIsNone(s.unidad_negocio)
            self.assertEqual(s.tipo, TipoScript.POWERSHELL)

    def test_es_idempotente(self):
        """Se corre en cada despliegue nuevo; no puede ir dejando copias."""
        call_command('seed_scripts_hora')
        call_command('seed_scripts_hora')
        self.assertEqual(Script.objects.filter(categoria='Hora').count(), 2)

    def test_el_de_diagnostico_no_cambia_nada(self):
        """Es la mitad del valor: poder mirar antes de tocar. Si se le colara un comando
        que escribe, dejaría de ser seguro correrlo sobre toda la cadena."""
        call_command('seed_scripts_hora')
        contenido = Script.objects.get(nombre__startswith='Diagnóstico de hora').contenido
        for peligroso in ('tzutil /s', 'w32tm.exe /config', '/resync', 'net.exe time', 'Set-Service'):
            self.assertNotIn(peligroso, contenido)
        self.assertIn('w32tm /query /status', contenido)

    def test_el_de_sincronizar_corrige_zona_y_reloj(self):
        """Son dos problemas distintos: el reloj UTC puede estar perfecto y la hora local
        mostrarse mal porque la región quedó en otro país."""
        call_command('seed_scripts_hora')
        contenido = Script.objects.get(nombre__startswith='Sincronizar hora').contenido
        self.assertIn('tzutil /s', contenido)
        self.assertIn('w32tm.exe /config', contenido)
        self.assertIn('/resync', contenido)

    def test_deja_el_peer_configurado_y_no_solo_sincroniza_una_vez(self):
        """Corregir el reloj una vez no evita que se vuelva a desviar en semanas. El
        `/manualpeerlist` es lo que hace que Windows lo mantenga solo — sin eso volvemos
        a estar acá el mes que viene."""
        call_command('seed_scripts_hora')
        contenido = Script.objects.get(nombre__startswith='Sincronizar hora').contenido
        self.assertIn('/manualpeerlist:', contenido)

    def test_tiene_el_respaldo_de_net_time(self):
        """w32tm falla si el peer no responde NTP pero sí SMB. `net time` es el camino que
        ya se sabe que funciona en esta red (ver instalar-servicio.ps1)."""
        call_command('seed_scripts_hora')
        contenido = Script.objects.get(nombre__startswith='Sincronizar hora').contenido
        self.assertIn('net.exe time', contenido)

    def test_los_valores_quedan_sustituidos_en_el_contenido(self):
        """Las plantillas llevan marcadores; si alguno quedara sin reemplazar, el script
        correría contra un servidor llamado literalmente SERVIDOR_HORA."""
        call_command('seed_scripts_hora')
        for s in Script.objects.filter(categoria='Hora'):
            self.assertNotIn('ZONA_ESPERADA', s.contenido)
            self.assertNotIn('SERVIDOR_HORA', s.contenido)
        contenido = Script.objects.get(nombre__startswith='Sincronizar hora').contenido
        self.assertIn('farmaciasmia.int', contenido)
        self.assertIn('SA Pacific Standard Time', contenido)

    def test_la_zona_coincide_con_la_que_espera_el_panel(self):
        """El script corrige a la zona de Ecuador y el panel marca como incorrecta
        cualquier otra. Si los dos lados se separaran, el script "arreglaría" estaciones
        que el panel seguiría mostrando en rojo."""
        from apps.catalogo.models import Estacion as EstacionModelo

        call_command('seed_scripts_hora')
        contenido = Script.objects.get(nombre__startswith='Sincronizar hora').contenido
        # SA Pacific Standard Time es UTC-5, que es lo que el panel exige.
        self.assertIn('SA Pacific Standard Time', contenido)
        self.assertEqual(EstacionModelo.OFFSET_UTC_ESPERADO_MINUTOS, -300)

    def test_sin_superusuario_avisa_y_no_crea_nada(self):
        User.objects.all().delete()
        call_command('seed_scripts_hora')
        self.assertEqual(Script.objects.filter(categoria='Hora').count(), 0)


class DiagnosticoRegionalTests(TestCase):
    """La parte de configuración regional del script de diagnóstico.

    Es un problema DISTINTO del reloj: una estación puede tener la hora perfecta, la zona
    correcta, y aun así romper el POS si el separador decimal quedó en "," y lee "1.50"
    como mil quinientos.
    """

    def setUp(self):
        User.objects.create_superuser(username='u_regional', password='x' * 14)
        call_command('seed_scripts_hora')
        self.contenido = Script.objects.get(nombre__startswith='Diagnóstico de hora').contenido
        # Solo el código: los comentarios de PowerShell nombran HKCU justamente para
        # explicar por qué NO se usa, y buscarlo en el texto crudo daba un falso positivo.
        self.codigo = '\n'.join(
            linea for linea in self.contenido.splitlines() if not linea.lstrip().startswith('#')
        )

    def test_lee_los_perfiles_de_usuario_y_no_hkcu(self):
        """El agente corre como LocalSystem: mirar HKCU mostraría el perfil de SYSTEM y no
        el del cajero, o sea reportaría "todo bien" sobre una estación rota. Por eso se
        recorre HKEY_USERS.
        """
        self.assertIn('HKEY_USERS', self.codigo)
        self.assertNotIn('HKCU', self.codigo)
        self.assertNotIn('HKEY_CURRENT_USER', self.codigo)

    def test_reporta_los_valores_que_le_importan_al_pos(self):
        """El separador decimal y el símbolo de moneda son los que deciden si el POS
        interpreta bien un precio; los de fecha, si interpreta bien una venta."""
        for clave in ('sDecimal', 'sThousand', 'sCurrency', 'sShortDate', 'sShortTime',
                      'iCurrDigits', 'LocaleName'):
            self.assertIn(clave, self.contenido)

    def test_resuelve_el_sid_a_un_nombre_de_usuario(self):
        """Un SID suelto no le dice nada a quien lee la salida: hay que poder saber si el
        perfil mal configurado es el del cajero o uno de servicio."""
        self.assertIn('SecurityIdentifier', self.contenido)
        self.assertIn('NTAccount', self.contenido)

    def test_sigue_sin_escribir_nada(self):
        """Agregarle la lectura del registro no puede haberlo vuelto peligroso: el valor
        de este script es poder lanzarlo sobre toda la cadena sin pensarlo."""
        for peligroso in ('Set-ItemProperty', 'New-ItemProperty', 'Remove-Item',
                          'tzutil /s', 'w32tm.exe /config', 'Set-Service', 'net.exe time'):
            self.assertNotIn(peligroso, self.codigo)
        self.assertIn('Get-ItemProperty', self.codigo)


class SincronizarVerificaElResultadoTests(TestCase):
    """El script no puede anunciar un arreglo que no ocurrió.

    El 14-sep-2026 la primera versión devolvió código 0 en cinco estaciones y ninguna
    sincronizó: el panel siguió midiendo los mismos desfases (+34, +96, −103, +102) y el
    propio `w32tm /query /status` decía "sin sincronizar", con el origen en
    "Free-running System Clock". El script había confiado en `$LASTEXITCODE`.

    Un script que miente sobre el resultado es peor que uno que falla: deja a todos
    creyendo que el problema está resuelto y nadie vuelve a mirarlo.
    """

    def setUp(self):
        User.objects.create_superuser(username='u_verif', password='x' * 14)
        call_command('seed_scripts_hora')
        self.contenido = Script.objects.get(nombre__startswith='Sincronizar hora').contenido
        self.codigo = '\n'.join(
            linea for linea in self.contenido.splitlines() if not linea.lstrip().startswith('#')
        )

    def test_no_anuncia_exito_sin_verificar(self):
        """La frase vieja afirmaba el resultado a partir del código de salida."""
        self.assertNotIn('Hora sincronizada via w32tm', self.codigo)

    def test_comprueba_el_origen_real_del_reloj(self):
        """`Free-running System Clock` y `Local CMOS Clock` significan que el equipo NO
        tiene fuente de tiempo. w32tm los devuelve en inglés aun en Windows en español."""
        self.assertIn('Free-running', self.codigo)
        self.assertIn('CMOS', self.codigo)
        self.assertIn('/query /source', self.codigo)

    def test_el_respaldo_depende_de_la_verificacion_y_no_del_codigo_de_salida(self):
        """`net time` tiene que dispararse cuando la comprobación dice que no sincronizó,
        no cuando w32tm devuelve un número distinto de cero."""
        indice_verificacion = self.codigo.index('Esta-Sincronizado')
        indice_net_time = self.codigo.index('net.exe time')
        self.assertLess(
            indice_verificacion, indice_net_time,
            'el respaldo tiene que ir después de comprobar el estado real',
        )

    def test_informa_la_hora_antes_y_despues(self):
        """Sin el antes y el después no hay forma de distinguir un reloj corregido de un
        script que tardó unos segundos en correr."""
        self.assertIn('Antes', self.contenido)
        self.assertIn('Despues', self.contenido)

    def test_deja_claro_que_la_confirmacion_la_da_el_panel(self):
        self.assertIn('el panel', self.contenido)

    def test_compara_la_zona_por_nombre_y_no_por_offset(self):
        """Ecuador y la zona Este de México son las dos UTC-5: comparar el offset daría
        por buena una región equivocada (el caso real de ML016-B)."""
        self.assertIn('$zonaActual -ne $ZonaEsperada', self.codigo)


class SeedActualizaScriptExistenteTests(TestCase):
    """`--actualizar` para pisar el contenido de un script ya sembrado.

    Hizo falta el 14-sep-2026: la primera versión del script de sincronizar anunciaba un
    éxito que no ocurría, y ya estaba cargada en producción. `get_or_create` la dejaba
    intacta por ser idempotente — correcto por defecto, porque el contenido de un script
    es ejecutable y alguien pudo haberlo ajustado a mano, pero deja sin camino la
    corrección de un script equivocado.
    """

    def setUp(self):
        User.objects.create_superuser(username='u_act', password='x' * 14)
        call_command('seed_scripts_hora')
        self.script = Script.objects.get(nombre__startswith='Sincronizar hora')
        self.contenido_bueno = self.script.contenido
        self.script.contenido = 'Write-Output "version vieja y equivocada"'
        self.script.save(update_fields=['contenido'])

    def test_sin_el_flag_no_pisa_nada(self):
        """El default tiene que seguir siendo no tocar: el seed corre en cada despliegue."""
        call_command('seed_scripts_hora')
        self.script.refresh_from_db()
        self.assertEqual(self.script.contenido, 'Write-Output "version vieja y equivocada"')

    def test_con_el_flag_restaura_el_contenido(self):
        call_command('seed_scripts_hora', '--actualizar')
        self.script.refresh_from_db()
        self.assertEqual(self.script.contenido, self.contenido_bueno)

    def test_no_duplica_el_script(self):
        call_command('seed_scripts_hora', '--actualizar')
        self.assertEqual(Script.objects.filter(nombre=self.script.nombre).count(), 1)

    def test_no_reescribe_el_historial_de_ejecuciones(self):
        """Cada EjecucionScript guarda su propio `contenido_snapshot`: actualizar la
        biblioteca no puede cambiar qué se ejecutó realmente aquella vez."""
        from apps.catalogo.models import Farmacia, Grupo, UnidadNegocio
        from apps.scripts.models import EjecucionScript

        unidad = UnidadNegocio.objects.get(codigo='SG')
        Farmacia.objects.create(
            codigo='ML001', grupo=Grupo.objects.create(codigo='TRX001'), unidad_negocio=unidad,
        )
        ejecucion = EjecucionScript.objects.create(
            script=self.script, contenido_snapshot=self.script.contenido,
            destino_tipo=EjecucionScript.DestinoTipo.ESTACIONES, unidad_negocio=unidad,
            creado_por=User.objects.first(),
        )

        call_command('seed_scripts_hora', '--actualizar')

        ejecucion.refresh_from_db()
        self.assertEqual(ejecucion.contenido_snapshot, 'Write-Output "version vieja y equivocada"')
