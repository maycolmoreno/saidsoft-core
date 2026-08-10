r"""Envoltorio de servicio de Windows para el agente SAIDSOFT (agente_prueba.py).

Necesario para producción: un agente instalado como servicio de Windows arranca solo
al bootear (sin que nadie inicie sesión) y Windows lo reinicia solo si crashea — misma
garantía que tenía el agente C# original vía `sc.exe failure ... actions=
restart/30000/restart/60000/restart/120000`, que acá configura instalar-servicio.ps1
después de registrar el servicio (pywin32 no expone eso directamente).

Requiere pywin32 (`pip install pywin32`, y correr una vez
`python Scripts\pywin32_postinstall.py -install` en la máquina de build) y que
agente_prueba.spec incluya sus imports ocultos al compilar con PyInstaller (ver
build.ps1).

Lee la configuración de conexión de config.json, junto al ejecutable (mismo patrón que
appsettings.Production.json del agente C# original) en vez de reusar argparse de
agente_prueba.py: un servicio de Windows no recibe argumentos de línea de comandos
cómodamente (el SCM los administra aparte, para install/start/stop/remove).

Comandos (correr como Administrador, desde la carpeta de instalación):
    Saidsoft.Agente.exe --startup auto install   — registra el servicio (arranque automático)
    Saidsoft.Agente.exe start                    — lo arranca
    Saidsoft.Agente.exe stop                     — lo detiene
    Saidsoft.Agente.exe remove                   — lo desregistra

pywin32 gestiona esos verbos solo al correr este módulo (o el .exe que lo empaqueta)
como entrypoint — no hace falta un dispatcher a mano.
"""
import json
import os
import sys
import threading

import servicemanager
import win32event
import win32service
import win32serviceutil

from agente_prueba import ARCHIVO_LOG, AgentePrueba, args_desde_config, _configurar_logging_archivo


def _ruta_config() -> str:
    base = os.path.dirname(os.path.abspath(sys.argv[0]))
    return os.path.join(base, 'config.json')


def _cargar_config() -> dict:
    with open(_ruta_config(), encoding='utf-8') as f:
        return json.load(f)


class ServicioAgenteSaidsoft(win32serviceutil.ServiceFramework):
    _svc_name_ = 'SaidsoftAgente'
    _svc_display_name_ = 'Saidsoft Agente'
    _svc_description_ = (
        'Agente de despliegue y monitoreo SAIDSOFT (enrolamiento, heartbeat, scripts, '
        'catálogo de software y despliegues de POS).'
    )

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.evento_parada = win32event.CreateEvent(None, 0, 0, None)
        self.agente = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        if self.agente is not None:
            self.agente.detener()
        win32event.SetEvent(self.evento_parada)

    def SvcDoRun(self):
        self.ReportServiceStatus(win32service.SERVICE_START_PENDING)

        # El SCM no siempre arranca el proceso con cwd = carpeta del ejecutable —
        # identidad.json/config.json/el log deben quedar juntos ahí, sin importar desde
        # dónde lo invoque Windows.
        os.chdir(os.path.dirname(os.path.abspath(sys.argv[0])))
        # No usar agente_prueba._configurar_logging(): agrega un StreamHandler(stdout),
        # y un servicio de Windows no tiene consola — escribir ahí puede tirar una
        # excepción. Solo archivo.
        _configurar_logging_archivo(ARCHIVO_LOG)

        try:
            args = args_desde_config(_cargar_config())
        except Exception as exc:
            servicemanager.LogErrorMsg(f'Saidsoft Agente: no se pudo leer config.json ({exc})')
            win32event.SetEvent(self.evento_parada)
            return

        servicemanager.LogInfoMsg(f'Saidsoft Agente: iniciando para la estación {args.codigo}...')
        self.agente = AgentePrueba(args)
        threading.Thread(target=self.agente.correr, daemon=True).start()
        self.ReportServiceStatus(win32service.SERVICE_RUNNING)

        win32event.WaitForSingleObject(self.evento_parada, win32event.INFINITE)
        servicemanager.LogInfoMsg('Saidsoft Agente: detenido.')


if __name__ == '__main__':
    win32serviceutil.HandleCommandLine(ServicioAgenteSaidsoft)
