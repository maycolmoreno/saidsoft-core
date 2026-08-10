# -*- mode: python ; coding: utf-8 -*-
# Genera dos ejecutables:
#   dist/agente_prueba.exe   — modo consola manual (pruebas, --codigo/--host/...).
#   dist/Saidsoft.Agente.exe — mismo agente envuelto como servicio de Windows
#                              (servicio_windows.py), para instalar con
#                              instalar-servicio.ps1. Lee config.json en vez de argv.
#
# hiddenimports de pywin32: PyInstaller no detecta solo estos módulos porque
# pywintypes/win32serviceutil los importan de forma dinámica (win32timezone en
# particular es un import perezoso adentro de pywintypes para manejar zonas horarias;
# sin declararlo acá, el .exe compila pero el servicio explota al arrancar con
# "ModuleNotFoundError: No module named 'win32timezone'" — no se ve en consola,
# solo en el Visor de eventos).
HIDDEN_IMPORTS_PYWIN32 = [
    'win32timezone',
    'win32serviceutil',
    'win32service',
    'win32event',
    'servicemanager',
]

a_consola = Analysis(
    ['agente_prueba.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz_consola = PYZ(a_consola.pure)

exe_consola = EXE(
    pyz_consola,
    a_consola.scripts,
    a_consola.binaries,
    a_consola.datas,
    [],
    name='agente_prueba',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

a_servicio = Analysis(
    ['servicio_windows.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=HIDDEN_IMPORTS_PYWIN32,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz_servicio = PYZ(a_servicio.pure)

exe_servicio = EXE(
    pyz_servicio,
    a_servicio.scripts,
    a_servicio.binaries,
    a_servicio.datas,
    [],
    name='Saidsoft.Agente',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    # False (windowed), no True: un exe de subsistema consola intenta interactuar con
    # una consola al arrancar, y la Sesión 0 (donde corre cualquier servicio de
    # Windows, sin escritorio) no tiene ninguna — el proceso nunca llega a levantar
    # (SCM error 1053 "no respondió a tiempo", sin excepción ni evento de crash en
    # ningún lado, reproducido tanto con LocalSystem como con una cuenta de servicio
    # dedicada — descarta que fuera un tema de permisos/cuenta). Corriendo
    # "install/start/stop/debug" a mano desde una consola de Administrador, Windows sigue
    # mostrando la salida igual (un proceso windowed heredado desde una consola existente
    # puede escribir a ella).
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
