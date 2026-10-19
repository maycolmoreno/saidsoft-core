"""Comandos de instalación y desinstalación silenciosa deducidos del propio instalador.

Antes había que escribirlos a mano en cada versión del catálogo y cada tipo de instalador
pide otros (`/S`, `/qn`, `/VERYSILENT`...): había que ir a buscarlos cada vez (pedido del
19-oct-2026, al cargar Firefox y LibreOffice). Ahora, si el campo queda vacío, se
completan solos según el tipo detectado.

**Instalación:** el tipo se reconoce por firmas binarias que pone la herramienta que armó
el instalador, no por el nombre del archivo:

- MSI: es un archivo compuesto OLE (`D0 CF 11 E0...` al principio).
- NSIS: el encabezado de datos lleva `0xDEADBEEF` + `NullsoftInst`.
- Inno Setup: `rDlPtS` (tabla del cargador) o `Inno Setup Setup Data`.
- WiX Burn (los "bundle" .exe que envuelven MSI): una sección PE llamada `.wixburn`.
- Instalador completo de Firefox: un SFX de 7-Zip (`;!@Install@!UTF-8!`) de Mozilla que le
  pasa los argumentos a su setup NSIS de adentro, así que también acepta `/S`.

Lo que no se reconoce NO se adivina: el formulario pide el comando. InstallShield queda
afuera a propósito, sus argumentos cambian según cómo lo empaquetó cada fabricante.

**Desinstalación:** un comando genérico que busca la aplicación por nombre en la clave
Uninstall del registro (64 y 32 bits) y usa lo que Windows sabe de ella: `msiexec /x` con
su código de producto si es MSI, `QuietUninstallString` si el fabricante la dejó, o la
`UninstallString` con los argumentos silenciosos del tipo. Así no hace falta conocer el
código de producto (cambia en cada versión de un MSI) ni la ruta del desinstalador.

El script evita las comillas dobles a propósito: el agente lo corre por `cmd` dentro de
`powershell -Command "..."`, y una comilla doble adentro cortaría el argumento.
Tampoco usa `-EncodedCommand`, que lo resolvería pero es una firma clásica que los EDR
bloquean (mismo motivo que en `_aplicar_actualizacion_agente` del agente).
"""
from apps.catalogo.instalador import _escapar_comillas_powershell

# Las firmas de NSIS, Inno y el SFX de Mozilla pueden estar después del cargador; los
# cargadores reales miden de decenas a cientos de KB.
BYTES_A_INSPECCIONAR = 8 * 1024 * 1024

MSI = 'msi'
NSIS = 'nsis'
INNO = 'inno'
BURN = 'burn'

NOMBRES_TIPO = {
    MSI: 'MSI', NSIS: 'NSIS', INNO: 'Inno Setup', BURN: 'WiX Burn',
}

_INSTALAR = {
    MSI: 'msiexec /i "{archivo}" /qn /norestart ALLUSERS=1',
    NSIS: '"{archivo}" /S',
    INNO: '"{archivo}" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-',
    BURN: '"{archivo}" /quiet /norestart',
}

# Lo que se agrega a la UninstallString cuando el fabricante no dejó una silenciosa.
_ARGUMENTOS_DESINSTALAR = {
    NSIS: '/S',
    INNO: '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART',
    BURN: '/quiet /norestart',
    MSI: '',
}


def detectar_tipo(cabecera: bytes) -> str | None:
    """Tipo de instalador según sus primeros bytes, o None si no se reconoce."""
    if cabecera.startswith(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'):
        return MSI
    if not cabecera.startswith(b'MZ'):
        return None
    # Burn primero: un bundle puede llevar adentro textos de otros instaladores.
    if b'.wixburn' in cabecera[:4096]:
        return BURN
    if b'\xef\xbe\xad\xdeNullsoftInst' in cabecera:
        return NSIS
    if b'rDlPtS' in cabecera or b'Inno Setup Setup Data' in cabecera:
        return INNO
    if b';!@Install@!UTF-8!' in cabecera and b'Mozilla' in cabecera:
        return NSIS
    return None


def leer_cabecera(archivo) -> bytes:
    """Los primeros bytes de un archivo subido, sin dejarlo movido."""
    archivo.seek(0)
    cabecera = archivo.read(BYTES_A_INSPECCIONAR)
    archivo.seek(0)
    return cabecera


def comando_instalacion(tipo: str) -> str:
    return _INSTALAR[tipo]


def comando_desinstalacion(nombre_aplicacion: str, tipo: str | None) -> str:
    """Desinstalación por nombre, para cualquier tipo (también uno no detectado: si la
    entrada del registro es MSI o tiene QuietUninstallString, alcanza igual)."""
    # Sin comillas dobles: ver el docstring del módulo. Las simples se escapan para
    # PowerShell, que también corta la cadena con las tipográficas.
    nombre = _escapar_comillas_powershell(nombre_aplicacion.replace('"', '').strip())
    extra = _ARGUMENTOS_DESINSTALAR.get(tipo, '')
    script = (
        "$q=[char]34;"
        "$k=Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*',"
        "'HKLM:\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*' -ErrorAction SilentlyContinue"
        f" | Where-Object {{ $_.DisplayName -and $_.DisplayName.IndexOf('{nombre}',"
        " [StringComparison]::OrdinalIgnoreCase) -ge 0 } | Select-Object -First 1;"
        f" if (-not $k) {{ [Console]::Error.WriteLine('No se encontro {nombre} en el registro'); exit 1 }};"
        " if ($k.WindowsInstaller -eq 1) { $a='/x '+$k.PSChildName+' /qn /norestart'; $f='msiexec.exe' }"
        " elseif ($k.QuietUninstallString) { $a='/c '+$q+$k.QuietUninstallString+$q; $f='cmd.exe' }"
        f" else {{ $a='/c '+$q+$k.UninstallString+' {extra}'+$q; $f='cmd.exe' }};"
        " $p=Start-Process -FilePath $f -ArgumentList $a -Wait -PassThru -WindowStyle Hidden;"
        " exit $p.ExitCode"
    )
    return f'powershell -NoProfile -NonInteractive -Command "{script}"'
