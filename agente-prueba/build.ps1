# Compila agente_prueba.py a un .exe standalone (no necesita Python instalado en la
# estación de prueba). Requiere el venv del proyecto principal con pyinstaller instalado:
#   ..\.venv\Scripts\python.exe -m pip install -r requirements.txt
#
# Uso: desde esta carpeta, .\build.ps1
# Resultado: dist\agente_prueba.exe

& "..\.venv\Scripts\pyinstaller.exe" --onefile --console --name agente_prueba --clean agente_prueba.py

Write-Host ""
Write-Host "Listo: dist\agente_prueba.exe"
Write-Host "Copiar ese único archivo a la estación de prueba y correrlo con --codigo, --host, etc."
Write-Host "Ver README.md de esta carpeta para el resto de las opciones."
