# SAIDSOFT — núcleo (Fase 1 y 2)

Reemplazo de `projectDJango` (Django 1.8/Python 2.7) + `projectNodeJS` del sistema
original (código de referencia en `C:\Proyectos\SAIDSOFT`, sin tocar), sobre
Django 5.2 LTS / Python 3.14. Ver [PLAN_MODERNIZACION.md](PLAN_MODERNIZACION.md)
para el panorama completo — esta es la copia viva del plan, se actualiza aquí.

Este es un proyecto independiente: no comparte carpeta con el sistema viejo.

## Estructura

```
config/                  settings (base/desarrollo/produccion), urls, wsgi/asgi
apps/catalogo/           Grupo (TRX), Farmacia, Estación
apps/despliegues/        Despliegue, ResultadoDespliegue, EventoDespliegue (línea de tiempo inmutable)
apps/auditoria/          EventoAuditoria (acciones del panel) + registrar_evento()
apps/mqtt_worker/        worker MQTT (reemplaza projectNodeJS/index.js) + simulador de agente
apps/panel/               panel HTMX: dashboard, estaciones, despliegues, auditoría (Fase 2)
templates/panel/          plantillas del panel (Tailwind + HTMX)
static_src/input.css      fuente de Tailwind (@source apunta a templates/ y apps/)
static/css/app.css        CSS compilado (versionado, no requiere Node en el servidor)
static/js/htmx.min.js     HTMX vendorizado (sin CDN)
tools/                    tailwindcss.exe standalone (NO versionado, ver abajo)
```

El **panel HTMX** (`/`) es la interfaz principal desde la Fase 2. El **Django admin**
(`/admin/`) se mantiene como vista avanzada/de respaldo (línea de tiempo detallada
de cada despliegue, edición directa de catálogos).

## Arrancar en local

```bash
python -m venv .venv
source .venv/Scripts/activate        # Windows/git-bash
pip install -r requirements-dev.txt  # incluye el broker MQTT embebido para pruebas locales
cp .env.example .env                 # completar SECRET_KEY, etc.

python manage.py migrate
python manage.py seed_demo           # carga TRX001/ML001 y TRX004/MAM01
python manage.py createsuperuser
```

Necesitas **tres procesos corriendo en paralelo**:

```bash
amqtt                                # broker MQTT (solo desarrollo; en prod: EMQX/Mosquitto)
python manage.py runserver --noreload  # panel web (http://localhost:8000/)
python manage.py run_mqtt_worker     # escucha enrolamiento/heartbeat/estado de despliegues
```

(`--noreload` evita que el autoreload de Django deje procesos hijos huérfanos en Windows/git-bash;
si editas código con el server corriendo, reinícialo a mano.)

## Modificar estilos (Tailwind)

El CLI standalone de Tailwind (~110MB) no se versiona. Para descargarlo de nuevo:

```bash
curl -sL -o tools/tailwindcss.exe \
  https://github.com/tailwindlabs/tailwindcss/releases/latest/download/tailwindcss-windows-x64.exe
```

Y recompilar tras tocar templates o `static_src/input.css`:

```bash
./tools/tailwindcss.exe -i static_src/input.css -o static/css/app.css --minify
```

`static/css/app.css` sí se versiona (es el artefacto final), así que producción
no necesita Node.js ni el binario de Tailwind — solo sirve el CSS ya compilado.

## Probar el flujo completo sin el agente C# (Fase 3, aún no construido)

```bash
python manage.py simular_agente ML001-A
python manage.py simular_agente MAM01-B --forzar-error   # prueba el rollback
```

El simulador se enrola, se suscribe a los tópicos MQTT de su farmacia/grupo/cadena,
descarga el `.zip` del despliegue publicado, verifica el hash y reporta cada paso del
ciclo (`recibido → descargado → hash_verificado → pos_cerrado → aplicado → pos_relanzado → ok`,
o `error → rollback`) — exactamente lo que hará el agente real más adelante.

Para probar el enrolamiento de una estación nueva (cola de aprobación del panel):

```bash
python -c "
import paho.mqtt.publish as p, json
p.single('/saidsof/enrolamiento/solicitar/', json.dumps({
    'codigo': 'ML001-B', 'numero_serie': 'SN-001',
    'so_nombre': 'Windows 11', 'so_build': '24H2', 'version_agente': '1.0.0'
}), hostname='localhost', port=1883)
"
```

## Notas de diseño

- **Aprobación de cuatro ojos**: quien crea un despliegue no puede aprobarlo — verificado
  tanto en `apps/panel/views.py::despliegue_aprobar` como en el admin.
- **Freno automático**: si el % de estaciones en error supera `umbral_error_pct`, el despliegue
  pasa a `pausado` solo (`apps/despliegues/services.py::evaluar_freno_automatico`).
- **Mensajes MQTT con `retain=True`**: una estación apagada al momento de publicar el
  despliegue lo recibe igual al encender.
- `version_pos` de una `Estacion` se actualiza tanto por heartbeat como al confirmar `ok`
  de un despliegue (no espera al siguiente heartbeat para reflejar la realidad).
- **Dashboard en vivo sin Channels**: el progreso de despliegues y la cola de aprobación
  se refrescan con `hx-trigger="every Ns"` (polling HTMX), no WebSockets — mucho menos
  infraestructura que Django Channels y suficiente a esta escala. Si más adelante se
  necesita latencia menor a 1s, ahí sí vale migrar a Channels.
- **Panel vs. admin**: el panel cubre el flujo diario (crear/aprobar/publicar despliegues,
  aprobar estaciones, ver cumplimiento). El admin sigue siendo el lugar para la línea de
  tiempo completa evento por evento de un `ResultadoDespliegue` y edición fina de catálogos.
