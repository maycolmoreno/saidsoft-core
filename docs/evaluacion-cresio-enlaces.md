# Evaluación de `Cresio_enlaces` frente al monitoreo proactivo de SAIDSOFT

**Fecha:** 11-sep-2026
**Pregunta que responde:** ¿el sistema que está en `C:\Users\ronald.moreno\Downloads\Nueva
carpeta\Nueva carpeta (2)` sirve para el proyecto grande de monitoreo proactivo?

**Respuesta corta:** sí, pero **no como código a adoptar** — el código fuente que importa no
está en esa carpeta. Sirve como *especificación probada en producción* y, sobre todo, porque
responde una pregunta que hoy bloquea el rollout entero de SAIDSOFT.

---

## 1. Qué hay realmente en esa carpeta

Dos proyectos, **ambos incompletos**:

| | Qué llegó | Qué falta |
|---|---|---|
| `backend/` | Scripts de orquestación (`run_app.py`, `run_monitor.py`), importadores de CSV, `setup_database.py`, 5 archivos de prueba, `requirements.txt`, ~70 MB de logs | **Todo el paquete `app/`**: `app.main`, `app.models`, `app.database`, `app.crud`, `app.redis_client`, `monitoring_manager` |
| `Cresio_enlaces/` | Solo configuración: `package.json`, `tsconfig*`, `vite`/`tailwind`/`eslint`, `index.html` | **Todo `src/`**: ningún componente, ninguna vista, ningún cliente de API |

`run_app.py` arranca `app.main:app` y `run_monitor.py` importa `app.database`, `app.models`,
`app.crud` y `app.redis_client`. Nada de eso está. **Tal como está, esa carpeta no arranca.**

Es el mismo patrón que ya pasó con el agente C# original (§10-K del plan): el código vive en
otra máquina. Antes de decidir nada hay que ubicar el repositorio real.

## 2. Qué hacía, según los logs (esto sí es dato duro)

Los logs llegan hasta el **4-sep-2026** — una semana antes de esta evaluación. Estaba vivo.

- **704 sucursales distintas** monitoreadas. Es la flota completa (SAIDSOFT tiene 700
  farmacias modeladas), más sitios que no son farmacia (`PRESIDENCIA`, `SALA CRM`).
- **Cobertura nacional**: Manabí, El Oro, Guayas, Loja, Azuay, Esmeraldas, Pichincha, Los Ríos.
- **Ciclo de ~60 segundos** por sucursal.
- **10.059 líneas** de eventos de caída/inactividad solo en el log vigente.
- Emite por **WebSocket** a un panel en vivo (`[WS-ALERTA] Clientes WebSocket activos: N`).

**Qué mide exactamente:** hace ping ICMP (`pythonping`) a la **IP del proveedor** de cada
sucursal, no a los equipos de la LAN. Los CSV lo muestran:

- `SANGREGORIO.csv` (316 filas): `ip_proveedor` es un gateway concreto, ej. `192.168.102.1`.
- `Sucursales.csv` (239 filas, MIA): `Ip Provedor` es la subred, ej. `10.101.18.224/27`.

Ambos traen además `caracteristica` = el nombre del circuito (`sangregorio2-santana`), que es
lo que el proveedor pide al abrir un ticket.

**Stack**: FastAPI + SQLAlchemy + PostgreSQL + Redis + APScheduler + WebSockets, con alertas
por correo (las credenciales SMTP se guardan cifradas con Fernet en la base).

## 3. El hallazgo que más importa

El bloqueante nº 1 de SAIDSOFT, documentado en `CLAUDE.md`, es:

> **Sin ruta de red a las farmacias.** El servidor no alcanza sus IP privadas; sin VPN ningún
> agente en farmacia llega al broker. Es el bloqueante nº 1 del rollout y el único que no se
> resuelve con código.

Y `apps/monitoreo/mikrotik.py` dice lo mismo: el sondeo SNMP central no tiene ruta hacia esas
IP privadas, por eso el Mikrotik se sondea **desde el agente**, dentro de la LAN.

**Pero este monitor alcanzaba 704 sucursales en IP privadas hasta el 4 de septiembre.**

Eso no prueba que el NUC (`10.111.6.20`) tenga ruta — prueba que **alguna máquina de la
organización sí la tiene**. Cuál es esa máquina, y por qué llega, es la pregunta más valiosa
de todo este análisis:

- Si esa ruta se puede extender al NUC, **se destraba el rollout del agente** (8 de ~1.800
  estaciones hoy), que es el cuello de botella real del proyecto.
- Si no se puede, sigue habiendo una opción concreta: correr el sondeo de enlaces **en esa
  máquina** y que reporte a SAIDSOFT por HTTP/MQTT, en vez de exigir ruta directa.

**Acción pendiente #1: averiguar en qué host corría `run_monitor.py` y qué ruta/VPN usa.**
Sin esa respuesta, cualquier decisión sobre monitoreo proactivo se toma a ciegas.

## 4. Qué aporta que SAIDSOFT hoy no tiene

| Capacidad | `Cresio_enlaces` | `saidsoft-core` |
|---|---|---|
| Estado up/down de los 704 enlaces | ✅ en producción | ❌ no existe |
| Monitoreo **sin agente instalado** | ✅ ICMP central | ❌ todo depende del agente (8/1.800) o MeshCentral |
| Historial de caídas de la flota completa | ✅ meses de datos | ❌ solo de las 8 estaciones con agente |
| Panel en vivo por WebSocket | ✅ | ❌ el panel usa polling HTMX |

La diferencia de fondo: **SAIDSOFT monitorea desde adentro del equipo y `Cresio_enlaces`
monitorea el enlace desde afuera.** El primero no puede decir nada de una farmacia sin agente
—que hoy son ~1.792 de 1.800—; el segundo cubre las 704 sucursales desde el día uno y no
necesita instalar nada.

## 5. Qué NO aporta

Nada de lo que SAIDSOFT ya resolvió, y que este sistema no tiene: multi-tenancy por unidad de
negocio, RBAC, motor de alertas con reglas configurables, ventanas de mantenimiento,
inventario ITAM, despliegues, scripts remotos, auditoría inmutable.

Tampoco aporta un frontend reutilizable: el panel de SAIDSOFT es HTMX + Tailwind y este es
React 19 + Vite. Mezclarlos sería introducir un segundo stack de UI para una sola pantalla.

## 6. Recomendación

**No migrar a `Cresio_enlaces`, ni adoptar su código. Portar su *capacidad* a
`apps/monitoreo`, que es donde ya vive todo lo demás.**

Concretamente, y en este orden:

1. **Ubicar el repositorio real** (el `app/` y el `src/` que faltan) y averiguar en qué host
   corre. Sin esto no hay nada que decidir. Es la acción #1 de arriba.
2. ~~**Portar el sondeo ICMP**~~ — **hecho el 11-sep-2026**: `apps/monitoreo/enlaces.py`
   (sondeo + ingesta), modelos `EstadoEnlaceFarmacia` y `EventoEnlaceFarmacia`, y el comando
   `python manage.py sondear_enlaces`. Usa `Farmacia.ip_router`, que ya se carga desde el
   Excel de operaciones.

   Tres decisiones que conviene conocer:

   - **No se programó en Celery Beat**, a propósito. Desde el servidor central no hay ruta
     (ver punto 3 de este documento), así que programarlo ahí registraría 704 caídas falsas.
     Se corre a mano desde un host con ruta hasta que se responda la acción pendiente #1.
   - **Guarda de barrido sospechoso**: si ≥80 % del barrido falla, no escribe nada y lo
     reporta como problema de ruta. 704 farmacias no se caen a la vez. Es la red de
     seguridad que evita repetir el error de `sincronizar_ancho_banda_farmacias`.
   - **La caída no se declara al primer fallo** sino tras 3 sondeos fallidos seguidos
     (igual que hacía `Cresio_enlaces`), pero el evento se fecha en el **primer** fallo: si
     no, toda caída aparecería más corta de lo real y el dato no serviría para un SLA.

   `python manage.py sondear_enlaces --solo-probar` responde, sin escribir nada, la
   pregunta "¿este host llega a las farmacias?". Es la forma más rápida de encontrar la
   máquina que sí tiene ruta.

   **Panel `/monitoreo/enlaces/`** — hecho también el 11-sep-2026. El criterio inicial fue
   dejarlo para después de saber si habría datos, pero es al revés: el panel es justamente
   cómo se confirma que los datos llegan cuando alguien corra el sondeo desde el host con
   ruta, y el admin estaba pensado solo como respaldo.
3. **Conectarlo al motor de alertas que ya existe** (`ReglaAlerta`/`Alerta`) en vez de escribir
   uno nuevo. Ojo con una limitación conocida: `Alerta.estacion` es FK obligatoria hoy, y una
   alerta de enlace es de farmacia, no de estación — es el mismo obstáculo que ya frenó las
   alertas de ancho de banda por farmacia (v1 quedó como solo visibilidad por ese motivo).
4. **Rescatar el histórico de caídas** de su PostgreSQL antes de que alguien apague esa
   máquina. Son meses de datos reales de disponibilidad de 704 sitios; sirven para la línea
   base de cualquier SLA con los proveedores, y no se pueden reconstruir.

## 7. Riesgo si no se hace nada

Los logs se cortan el 4-sep-2026 y `run_monitor.pid` quedó escrito. No sabemos si el sistema
sigue corriendo o si se detuvo. Si se apagó, hoy **nadie está viendo el estado de los enlaces
de 704 sucursales**, y SAIDSOFT no puede cubrir ese hueco porque solo ve 8 estaciones.

**Acción pendiente #2: confirmar si `Cresio_enlaces` sigue en marcha.** Si se cayó, es una
pérdida de visibilidad operativa que nadie registró.
