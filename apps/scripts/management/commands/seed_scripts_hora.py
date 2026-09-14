"""Crea los scripts de biblioteca para diagnosticar y corregir la hora de una estación.

Por qué hace falta: el reloj corrido no es un problema cosmético. El agente descarta
todo mensaje firmado con más de 120 s de desfase (VENTANA_TIMESTAMP_SEGUNDOS, SEC-1),
así que pasada esa marca la estación ignora en silencio todos los scripts y comandos del
panel — incluido el que le arreglaría el reloj. El único rastro queda en su log local.

`instalar-servicio.ps1` ya configura W32Time al instalar el agente (parámetro
`-ServidorHora`, por defecto farmaciasmia.int), pero eso solo corre en la instalación.
Una estación ya instalada cuyo servicio de tiempo se detuvo o nunca se configuró no
tiene forma de arreglarse sin que alguien vaya al local. Estos scripts son ese camino,
y reusan la misma secuencia que el instalador porque es la que ya se sabe que funciona
en esta red (w32tm primero, `net time` de respaldo).

El de diagnóstico NO cambia nada: existe para poder mirar antes de tocar, que a 1.800
equipos es la diferencia entre saber y suponer. Además del reloj reporta la configuración
regional, que es un problema DISTINTO: una estación puede tener la hora perfecta y aun
así romper el POS si el separador decimal quedó en "," y lee "1.50" como mil quinientos.
Eso vive en `Control Panel > International`, que es por usuario, y el agente corre como
LocalSystem — por eso se recorre HKEY_USERS y no HKCU, que mostraría el perfil de SYSTEM.

    python manage.py seed_scripts_hora
"""
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.scripts.models import Script, TipoScript

CATEGORIA = 'Hora'

# Ecuador: UTC-5 todo el año, sin horario de verano.
ZONA_HORARIA = 'SA Pacific Standard Time'
SERVIDOR_HORA = 'farmaciasmia.int'

CONTENIDO_DIAGNOSTICO = r"""$ErrorActionPreference = 'Continue'

"Hora local       : $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
"Hora UTC         : $((Get-Date).ToUniversalTime().ToString('yyyy-MM-dd HH:mm:ss'))"
"Zona horaria     : $(tzutil /g)"
"Zona esperada    : ZONA_ESPERADA"
"En el dominio    : $((Get-CimInstance Win32_ComputerSystem).PartOfDomain)"

$svc = Get-Service W32Time -ErrorAction SilentlyContinue
if ($svc) {
    "Servicio W32Time : $($svc.Status), inicio $($svc.StartType)"
} else {
    "Servicio W32Time : NO EXISTE en esta estacion"
}

"--- w32tm /query /source ---"
w32tm /query /source
"--- w32tm /query /status ---"
w32tm /query /status

# Configuracion regional: es lo que decide si el POS lee "1.50" como un dolar cincuenta
# o como mil quinientos. Vive en Control Panel\International, que es POR USUARIO: el
# agente corre como LocalSystem, asi que mirar HKCU mostraria el perfil de SYSTEM y no
# el del cajero. Por eso se recorren los perfiles cargados en HKEY_USERS.
"--- Configuracion regional, por perfil de usuario ---"
$claves = @('LocaleName','sShortDate','sLongDate','sShortTime','sTimeFormat',
            'iFirstDayOfWeek','sCurrency','iCurrDigits','iNegCurr','sDecimal',
            'sThousand','sList','iMeasure')
Get-ChildItem Registry::HKEY_USERS -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -notmatch '_Classes$' } |
    ForEach-Object {
        $sid = $_.PSChildName
        $ruta = "Registry::HKEY_USERS\$sid\Control Panel\International"
        if (Test-Path $ruta) {
            try {
                $obj = New-Object System.Security.Principal.SecurityIdentifier($sid)
                $usuario = $obj.Translate([System.Security.Principal.NTAccount]).Value
            } catch {
                $usuario = '(sid sin resolver)'
            }
            ""
            "  $usuario   [$sid]"
            $v = Get-ItemProperty -Path $ruta
            foreach ($k in $claves) {
                if ($null -ne $v.$k) { "     {0,-16} {1}" -f $k, $v.$k }
            }
        }
    }
"""

CONTENIDO_SINCRONIZAR = r"""$ErrorActionPreference = 'Continue'
$ServidorHora = 'SERVIDOR_HORA'
$ZonaEsperada = 'ZONA_ESPERADA'

function Origen-Del-Reloj {
    (& w32tm.exe /query /source) -join ' '
}

function Esta-Sincronizado {
    # "Free-running System Clock" y "Local CMOS Clock" los devuelve w32tm en ingles aun
    # en un Windows en espanol, y significan lo contrario de sincronizado: el equipo
    # sigue con su reloj de hardware suelto, sin ninguna fuente.
    $origen = Origen-Del-Reloj
    -not ($origen -match 'Free-running' -or $origen -match 'CMOS')
}

"Antes  : $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  zona: $(tzutil /g)  origen: $(Origen-Del-Reloj)"

# 1) Zona horaria. Es un problema APARTE del desfase: el reloj UTC puede estar perfecto
# y la hora local mostrarse mal porque la region quedo en otro pais. Ademas, dos regiones
# distintas pueden compartir el mismo huso (Ecuador y la zona Este de Mexico son las dos
# UTC-5), asi que se compara el NOMBRE y no el offset.
$zonaActual = (tzutil /g)
if ($zonaActual -ne $ZonaEsperada) {
    tzutil /s $ZonaEsperada
    "Zona horaria corregida: $zonaActual -> $ZonaEsperada"
} else {
    "Zona horaria ya estaba correcta ($zonaActual)."
}

# 2) W32Time viene deshabilitado en algunas imagenes de Windows; sin esto, un
# 'w32tm /resync' falla con "El servicio no se ha iniciado".
Set-Service -Name W32Time -StartupType Automatic -ErrorAction SilentlyContinue
Start-Service -Name W32Time -ErrorAction SilentlyContinue

# 3) Se configura el peer ADEMAS de sincronizar ahora: corregir el reloj una vez no evita
# que se vuelva a desviar en semanas. Queda listo para cuando el servidor sirva NTP.
& w32tm.exe /config /manualpeerlist:$ServidorHora /syncfromflags:manual /update | Out-Null
& w32tm.exe /resync | Out-Null

# 4) NO se confia en el codigo de salida de w32tm. El 14-sep-2026 esta misma secuencia
# devolvio 0 en cinco estaciones y ninguna sincronizo: el panel seguia midiendo los
# mismos desfases y el propio 'query /status' decia "sin sincronizar". Un script que
# anuncia un arreglo que no ocurrio es peor que uno que falla, porque deja a todos
# creyendo que el problema esta resuelto. Se verifica el estado real.
if (Esta-Sincronizado) {
    "w32tm quedo sincronizado contra $ServidorHora."
} else {
    "w32tm NO sincronizo (el origen sigue siendo el reloj local). Se aplica 'net time'..."
    # net time va por SMB, no por NTP. En esta red el servidor no responde NTP (UDP/123
    # sin servicio, verificado desde dos puntos el 14-sep-2026) pero si responde SMB, asi
    # que este es hoy el unico camino que realmente corrige la hora.
    & net.exe time "\\$ServidorHora" /set /yes | Out-Null
    if ($LASTEXITCODE -eq 0) {
        "net time aplicado contra $ServidorHora."
    } else {
        "ERROR: no se pudo corregir la hora ni con w32tm ni con net time (codigo $LASTEXITCODE)."
    }
}

"Despues: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  zona: $(tzutil /g)  origen: $(Origen-Del-Reloj)"
"--- w32tm /query /status ---"
w32tm /query /status
""
"El desfase real lo confirma el panel en el proximo latido; esta salida no lo afirma."
"""


def _con_valores(plantilla: str) -> str:
    return plantilla.replace('ZONA_ESPERADA', ZONA_HORARIA).replace('SERVIDOR_HORA', SERVIDOR_HORA)


SCRIPTS = [
    (
        'Diagnóstico de hora, zona y formato regional',
        'Solo lectura: informa hora, zona horaria, estado del servicio W32Time, si la '
        'estación está en el dominio, y la configuración regional (separador decimal, '
        'moneda, formato de fecha) de cada perfil de usuario. No cambia nada.',
        _con_valores(CONTENIDO_DIAGNOSTICO),
    ),
    (
        'Sincronizar hora con el dominio',
        f'Corrige la zona horaria a "{ZONA_HORARIA}" si hace falta, habilita W32Time y '
        f'sincroniza contra {SERVIDOR_HORA} (con "net time" de respaldo). Deja el peer '
        f'configurado para que Windows la mantenga en hora sola.',
        _con_valores(CONTENIDO_SINCRONIZAR),
    ),
]


class Command(BaseCommand):
    help = 'Crea los scripts compartidos de diagnóstico y sincronización de hora (biblioteca).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--actualizar', action='store_true',
            help='Pisa el contenido de los scripts que ya existan con el de esta versión. '
                 'Sin esto solo se crean los que faltan, y un script ya sembrado se deja '
                 'intacto — que es lo correcto por defecto: el contenido de un script es '
                 'ejecutable y alguien pudo haberlo ajustado a mano.',
        )

    @transaction.atomic
    def handle(self, *args, **options):
        admin = User.objects.filter(is_superuser=True).order_by('id').first()
        if admin is None:
            self.stderr.write(self.style.ERROR('Necesitas al menos un superusuario antes de correr este seed.'))
            return

        creados = actualizados = 0
        for nombre, descripcion, contenido in SCRIPTS:
            script, creado = Script.objects.get_or_create(
                nombre=nombre, unidad_negocio=None,
                defaults={
                    'descripcion': descripcion, 'tipo': TipoScript.POWERSHELL,
                    'contenido': contenido, 'categoria': CATEGORIA, 'creado_por': admin,
                },
            )
            creados += int(creado)
            estado = 'creado '
            if not creado and options['actualizar'] and script.contenido != contenido:
                # No toca `creado_por` ni la fecha de creación: el script sigue siendo el
                # mismo de la biblioteca, con el contenido corregido. Las ejecuciones ya
                # hechas guardan su propio `contenido_snapshot`, así que el historial no
                # se reescribe.
                script.contenido = contenido
                script.descripcion = descripcion
                script.categoria = CATEGORIA
                script.save(update_fields=['contenido', 'descripcion', 'categoria'])
                actualizados += 1
                estado = 'ACTUALIZADO'
            elif not creado:
                estado = 'ya existía'
            self.stdout.write(f'  {estado:<12} {nombre}')

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'{creados} creado(s), {actualizados} actualizado(s).',
        ))
        if not options['actualizar']:
            self.stdout.write(
                'Los que ya existían se dejaron intactos. Si venís de una versión anterior '
                'de estos scripts, repetí con --actualizar.',
            )
        self.stdout.write(self.style.WARNING(
            'Corré primero el de diagnóstico: cambiar la hora de un equipo con el POS abierto '
            'no es gratis, y conviene saber qué estación necesita el arreglo antes de aplicarlo.',
        ))
