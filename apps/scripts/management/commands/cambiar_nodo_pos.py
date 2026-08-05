"""Cambia el nodo TRX (clave "Bdd" del .Config de Zabyca) de todas las estaciones
aprobadas de un grupo, en una sola pasada: arma el script, lo registra como ejecución
ad-hoc y lo envía por MQTT — sin pasar por el panel a mano.

Reemplaza el proceso manual anterior (editar la línea a mano en cada estación,
re-comprimir el cliente y reenviarlo) — ver PLAN_MODERNIZACION.md §10-C y el README
de saidsoft-agente ("Nodos TRX y el .Config del POS").

Solo edita el .Config; NO cierra ni reinicia Zabyca.Pos.Desktop.exe (una estación
puede tener una venta en curso) — el cambio toma efecto en el próximo arranque del
POS, a criterio de la farmacia.

    python manage.py cambiar_nodo_pos --unidad-negocio SG --grupo TRX002 --nodo trx002 --usuario admin
"""
import re

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.catalogo.models import Grupo, UnidadNegocio
from apps.scripts.models import EjecucionScript
from apps.scripts.services import crear_script_adhoc, registrar_ejecucion_script

NODO_PATTERN = re.compile(r'^[A-Za-z0-9_-]{1,50}$')

# Mismo POS para SG/MIA/7DIAS (ver saidsoft-agente/deploy/instalar-agente.ps1).
CONFIG_PATH = r'C:\Program Files (x86)\Farmamia Cia Ltda - Elipsys\Cliente\Zabyca.Pos.Desktop.exe.Config'

CONTENIDO_TEMPLATE = r'''$ErrorActionPreference = "Stop"
$configPath = "{config_path}"
$nodoNuevo = "{nodo}"

if (-not (Test-Path $configPath)) {{
    Write-Error "No se encontro $configPath en esta estacion."
    exit 1
}}

$respaldo = "$configPath.bak-$(Get-Date -Format yyyyMMddHHmmss)"
Copy-Item $configPath $respaldo -Force

[xml]$xml = Get-Content $configPath
$nodo = $xml.SelectSingleNode("//add[@key='Bdd']")
if ($null -eq $nodo) {{
    Write-Error "No se encontro la clave 'Bdd' en $configPath (respaldo intacto en $respaldo)."
    exit 1
}}
$anterior = $nodo.value
$nodo.value = $nodoNuevo
$xml.Save($configPath)

Write-Output "Nodo cambiado: '$anterior' -> '$nodoNuevo' (respaldo: $respaldo)."
Write-Output "Zabyca.Pos.Desktop.exe NO se reinicio: el cambio toma efecto en el proximo arranque del POS."
'''


class Command(BaseCommand):
    help = 'Cambia el nodo TRX del POS (clave "Bdd") en todas las estaciones aprobadas de un grupo.'

    def add_arguments(self, parser):
        parser.add_argument('--unidad-negocio', required=True, help='Código de la unidad de negocio (ej. SG).')
        parser.add_argument('--grupo', required=True, help='Código del grupo/canal a re-nodar (ej. TRX002).')
        parser.add_argument('--nodo', required=True, help='Valor nuevo de la clave "Bdd" (ej. trx002).')
        parser.add_argument('--usuario', required=True, help='Username del operador que autoriza el cambio.')
        parser.add_argument('--timeout-segundos', type=int, default=120)

    def handle(self, *args, **options):
        nodo = options['nodo']
        if not NODO_PATTERN.match(nodo):
            raise CommandError(
                f'--nodo "{nodo}" inválido: solo letras/números/guion/guion bajo, máx. 50 caracteres '
                '(se inserta directo en un script PowerShell, no se aceptan comillas ni otros símbolos).'
            )

        try:
            unidad_negocio = UnidadNegocio.objects.get(codigo=options['unidad_negocio'])
        except UnidadNegocio.DoesNotExist:
            raise CommandError(f'No existe la unidad de negocio "{options["unidad_negocio"]}".')

        try:
            grupo = Grupo.objects.get(codigo=options['grupo'])
        except Grupo.DoesNotExist:
            raise CommandError(f'No existe el grupo "{options["grupo"]}".')

        usuario = get_user_model().objects.filter(username=options['usuario']).first()
        if usuario is None:
            raise CommandError(f'No existe el usuario "{options["usuario"]}".')

        contenido = CONTENIDO_TEMPLATE.format(config_path=CONFIG_PATH, nodo=nodo)
        script = crear_script_adhoc(
            nombre=f'Cambiar nodo POS a "{nodo}" — grupo {grupo.codigo}',
            tipo='powershell', contenido=contenido, unidad_negocio=unidad_negocio, usuario=usuario,
        )
        ejecucion = registrar_ejecucion_script(
            script=script, destino_tipo=EjecucionScript.DestinoTipo.GRUPOS, usuario=usuario,
            unidad_negocio=unidad_negocio, timeout_segundos=options['timeout_segundos'], grupos=[grupo],
        )

        total = ejecucion.resultados.count()
        enviados = ejecucion.resultados.filter(estado='enviado').count()
        self.stdout.write(self.style.SUCCESS(
            f'Ejecución #{ejecucion.pk}: nodo "{nodo}" enviado a {enviados}/{total} estación(es) '
            f'aprobada(s) del grupo {grupo.codigo}. Seguir el resultado en /ejecuciones/{ejecucion.pk}/.'
        ))
        if enviados < total:
            self.stdout.write(self.style.WARNING(
                f'{total - enviados} estación(es) no recibieron el comando (offline o error de publish) — '
                'revisar en el detalle de la ejecución.'
            ))
