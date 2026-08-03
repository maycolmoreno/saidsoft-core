---
name: levantar-proyecto-local
description: "Cómo arrancar SAIDSOFT localmente (los 3 procesos) en este entorno Windows/git-bash, y las trampas que no están en el README"
metadata: 
  node_type: memory
  type: project
  originSessionId: 0825270e-16be-4d7c-99e0-f74547688878
  modified: 2026-08-03T13:04:45.878Z
---

Para correr el panel completo localmente hacen falta 3 procesos en paralelo (ya documentado en README.md), pero el README no cubre estas trampas específicas de este entorno (Windows + git-bash + Bash tool en background):

**1. Sin `PYTHONUNBUFFERED=1`, no se ve nada del output.** `manage.py runserver` y `run_mqtt_worker` no imprimen ni el banner de arranque hasta que el proceso muere — el stdout queda buffereado cuando no hay TTY real detrás (nohup + redirección a archivo). Siempre lanzar así:
```bash
DJANGO_SETTINGS_MODULE=config.settings.desarrollo PYTHONUNBUFFERED=1 nohup ./.venv/Scripts/python.exe manage.py runserver --noreload 127.0.0.1:8000 > archivo.log 2>&1 &
disown
```

**2. `ps aux` no muestra los procesos python.exe de forma confiable en este entorno** (git-bash sobre Windows). Para verificar qué hay corriendo o para matar procesos de sesiones anteriores, usar herramientas nativas de Windows:
```bash
tasklist //FI "IMAGENAME eq python.exe" //FO CSV
wmic process where "name='python.exe'" get ProcessId,CommandLine   # para identificar cuál es cuál antes de matar
taskkill //PID <pid> //F
```
Verificar siempre la línea de comando completa antes de matar un PID — pueden convivir procesos de otras sesiones/proyectos del usuario.

**3. `seed_activos` exige un superusuario ya creado** — falla con "Necesitas al menos un superusuario antes de correr este seed" si se corre antes de `createsuperuser`. Orden correcto: `migrate` → `seed_demo` → `createsuperuser` (se puede no-interactivo con `DJANGO_SUPERUSER_USERNAME/EMAIL/PASSWORD` + `--noinput`) → `seed_activos` → `seed_permisos`.

**4. Extracción de CSRF token con curl+grep**: si se usa `grep -o` sobre el HTML para sacar el `csrfmiddlewaretoken`, hay que agregar `| head -1` — el token aparece repetido varias veces en la misma página (una vez por cada `{% csrf_token %}`, ej. formulario principal + posibles forms ocultos en base.html) y sin `head -1` se concatenan los duplicados en una sola variable corrupta (~3x la longitud real), lo que da "CSRF token from POST has incorrect length".

**Cómo aplica**: la próxima vez que pidan "levanta/corre el proyecto" en esta sesión, usar directamente esta receta en vez de redescubrir las trampas de nuevo.
