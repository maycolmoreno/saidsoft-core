# Compila agente_prueba.py y servicio_windows.py a dos .exe standalone (no necesitan
# Python instalado en la estación destino). Requiere el venv del proyecto principal con
# las dependencias de esta carpeta instaladas:
#   ..\.venv\Scripts\python.exe -m pip install -r requirements.txt
#   ..\.venv\Scripts\python.exe ..\.venv\Scripts\pywin32_postinstall.py -install   # una sola vez
#
# Uso: desde esta carpeta, .\build.ps1
# Resultado:
#   dist\agente_prueba.exe    — modo consola manual (pruebas, --codigo/--host/...)
#   dist\Saidsoft.Agente.exe  — mismo agente como servicio de Windows (config.json)
#
# IMPORTANTE: usa agente_prueba.spec (no --onefile/--name por CLI) porque el spec ya
# define AMBOS ejecutables y los hiddenimports de pywin32 que necesita el servicio
# (ver comentario en el spec) — invocar pyinstaller con la ruta al .py en vez del
# .spec lo regeneraría y perdería esa configuración.
& "..\.venv\Scripts\pyinstaller.exe" agente_prueba.spec --clean

Write-Host ""
Write-Host "Listo:"
Write-Host "  dist\agente_prueba.exe    - copiar a una estación y correr con --codigo, --host, etc. (pruebas)"
Write-Host "  dist\Saidsoft.Agente.exe  - instalar como servicio con instalar-servicio.ps1 (producción)"
Write-Host "Ver README.md de esta carpeta para el resto de las opciones."
