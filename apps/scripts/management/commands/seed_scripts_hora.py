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

"Antes  : $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  zona: $(tzutil /g)"

# 1) Zona horaria. Es un problema APARTE del desfase: el reloj UTC puede estar perfecto
# y la hora local mostrarse mal porque la region quedo en otro pais. Sincronizar no
# arregla eso, por eso se corrige por separado y solo si hace falta.
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

# 3) Se configura el peer ADEMAS de sincronizar ahora: corregir el reloj una vez no
# evita que se vuelva a desviar en semanas. Con el peer configurado, Windows lo mantiene
# sincronizado solo de ahi en adelante.
& w32tm.exe /config /manualpeerlist:$ServidorHora /syncfromflags:manual /update | Out-Null
& w32tm.exe /resync | Out-Null

if ($LASTEXITCODE -ne 0) {
    # w32tm puede fallar si el peer no responde NTP pero si SMB. 'net time' es el camino
    # que ya se sabe que funciona en esta red (ver instalar-servicio.ps1).
    "w32tm no pudo sincronizar (codigo $LASTEXITCODE); se intenta con 'net time'..."
    & net.exe time "\\$ServidorHora" /set /yes | Out-Null
    if ($LASTEXITCODE -ne 0) {
        "ERROR: no se pudo sincronizar contra $ServidorHora ni con w32tm ni con net time."
    } else {
        "Hora sincronizada via 'net time'."
    }
} else {
    "Hora sincronizada via w32tm contra $ServidorHora."
}

"Despues: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  zona: $(tzutil /g)"
"--- w32tm /query /status ---"
w32tm /query /status
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

    @transaction.atomic
    def handle(self, *args, **options):
        admin = User.objects.filter(is_superuser=True).order_by('id').first()
        if admin is None:
            self.stderr.write(self.style.ERROR('Necesitas al menos un superusuario antes de correr este seed.'))
            return

        creados = 0
        for nombre, descripcion, contenido in SCRIPTS:
            _, creado = Script.objects.get_or_create(
                nombre=nombre, unidad_negocio=None,
                defaults={
                    'descripcion': descripcion, 'tipo': TipoScript.POWERSHELL,
                    'contenido': contenido, 'categoria': CATEGORIA, 'creado_por': admin,
                },
            )
            creados += int(creado)
            self.stdout.write(f'  {"creado " if creado else "ya existía"}  {nombre}')

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'{creados} script(s) de hora creado(s) (los ya existentes se dejaron igual).',
        ))
        self.stdout.write(self.style.WARNING(
            'Corré primero el de diagnóstico: cambiar la hora de un equipo con el POS abierto '
            'no es gratis, y conviene saber qué estación necesita el arreglo antes de aplicarlo.',
        ))
