# Roadmap de SaidSoft

**Fecha:** 20-oct-2026 · **Base:** los cinco documentos de `docs/consolidacion/`.
**No se desarrolla nada de esto hasta que se apruebe.**

Cada fase dice qué necesidad verificada atiende, de qué depende, sus riesgos y cómo se acepta.
Las referencias (S-xx, E-xx, D-x, RR-x, CR-x) apuntan a `AUDITORIA_SEGURIDAD_ESTABILIDAD.md`,
`MATRIZ_CONSOLIDACION.md` y `AUDITORIA_FLUJO_ESTACION.md`.

**El principio que ordena todo:** la plataforma está construida y en uso parcial (85 estaciones
de unas 1.800 estimadas hoy [DOC]; a 1.300 farmacias serían unas 3.300, **ESTIMACIÓN** proporcional
de este documento); lo que limita es la
**operación y la infraestructura**, no la falta de funciones. Por eso las fases cierran riesgos y
consolidan antes de agregar.

---

## Fase 0 — Decisiones y verificaciones sin desarrollo

**Duración estimada:** días. **Lo hace:** el responsable de la operación.

| # | Acción | Por qué |
|---|---|---|
| 0.1 | **Decidir qué hacer con `68d0045`** (ya en `origin/master`): desplegarlo así (respalda EMQX; no sube a OneDrive mientras `BACKUP_ONEDRIVE_REMOTO` esté vacío) o revertirlo hasta la fase de respaldos | La sección 7 del pedido pospone OneDrive, y el commit ya está publicado |
| 0.2 | Guardar **`deploy/.env` y la passphrase de GPG** fuera del NUC, en un gestor de contraseñas | RR-5: sin eso ningún respaldo sirve |
| 0.3 | Confirmar que las estaciones aplicaron los catálogos publicados el 20-oct | §5 del flujo: NO VERIFICADO |
| 0.4 | Que un administrador con `sudo` lea el firewall y liste las API Keys de EMQX | S-03, S-13 |
| 0.5 | Revisar físicamente la temperatura del NUC | Reinicios por calor ya ocurridos |
| 0.6 | Responder tres preguntas: ¿los técnicos usan la app móvil? ¿hay cobro entre unidades (`facturacion`)? ¿la organización ya usa GLPI u otra herramienta de inventario o tickets? | Deciden la fase 4. La última es una **HIPÓTESIS**: el usuario del servidor se llama `glpi` |

**Se acepta cuando** cada punto tiene una respuesta escrita.

### Resultados (20-oct-2026)

| # | Resultado |
|---|---|
| 0.1 | **Hecho.** `68d0045` desplegado; un respaldo manual a las 19:46 generó base, archivos subidos y EMQX (14 KB). OneDrive sin configurar, según la sección 7 |
| 0.2 | **Hecho.** `.env` y `certs/` en un paquete cifrado con GPG, guardado como adjunto en KeePass, con una segunda copia del `.kdbx` en un USB aparte; copias temporales borradas del NUC y de la PC. Repetir cada vez que cambie el `.env` |
| 0.3 | Pendiente: verificar 24 h después de publicar |
| 0.4 | **Hecho.** El NUC no filtra nada (`DOCKER-USER` vacía, `ufw` inactivo): S-13 confirmado y 1.1 pasa a ser la primera medida de la fase 1. Dos API Keys de EMQX sin vencimiento: el `.env` usa `saidsoft-provisioning`, creada a mano; `saidsof_django_admin`, la del script del repositorio, no se usa |
| 0.5 | Pendiente: revisión física |
| 0.6 | **La app móvil todavía no se usa. No hay cobro entre unidades. No hay GLPI ni otra herramienta de inventario o tickets** |

**Qué cambia:**
- `facturacion` pasa a candidato a retirada.
- Mantenimiento e inventario van al proyecto separado sin usuarios de la app que proteger.
- La fila de ITAM de la tabla "Construir o reutilizar" queda resuelta: proyecto propio, porque no hay con qué integrarse.

---

## Fase 1 — Seguridad y continuidad

**Necesidades verificadas:** S-02, S-03, S-05 a S-08, RR-1 a RR-9.

| # | Trabajo | Tipo | Depende de |
|---|---|---|---|
| 1.1 | Dashboard de EMQX solo en `127.0.0.1:8082` o detrás del firewall (R-1) | Configuración | 0.4 |
| 1.2 | Respaldos fuera del NUC, **prueba de restauración**, MeshCentral y certificados en el respaldo, RPO y RTO definidos | Operación + script existente | 0.1, 0.2; autorización de Microsoft 365 o el destino que se elija |
| 1.3 | Rotar la contraseña del dashboard y las API Keys, con vencimiento (R-8). **Desactivar** `saidsof_django_admin` (no borrarla) y alinear `bootstrap-emqx.sh` con la clave que usa producción (`saidsoft-provisioning`) | Operación + script | 1.1 |
| 1.4 | MFA obligatorio para los roles Administrador y Operador RMM; token de la API con vencimiento o atado al MFA | Código chico | — |
| 1.5 | MeshCentral con versión fija de imagen; decidir si se filtra 8083 | Configuración | — |
| 1.6 | Completar `deploy/.env.prod.example` con las 22 variables que faltan | Documentación | — |

**Riesgos:** 1.1 y 1.3 podrían cortar el aprovisionamiento de credenciales si `EMQX_API_URL` no
fuera interno. **Verificado el 20-oct: es `http://emqx:18083`**, así que 1.1 no lo afecta. Los
scripts del host usan `localhost:8082`: al aplicar 1.1, cambiarlos a `127.0.0.1` o publicar también
`[::1]` (laboratorio C1, F-F/F-G).

**Datos para 1.4 y 1.5 (20-oct):**
- Ninguna de las 3 cuentas con privilegios tiene MFA, y una se llama `prueba`.
- MeshCentral corre la versión 1.2.4, digest
  `ghcr.io/ylianst/meshcentral@sha256:cd40bdda9615bc57bddfa8791877608b1b48b5dab3bef85d98a3bf6f162a50c8`.
  Fijar ese digest en el compose (1.5) no cambia la imagen que corre hoy.

**Se acepta cuando:**
- desde otra red, 8082 no responde;
- existe una restauración probada desde la copia externa, con la cantidad de tablas y filas verificada;
- ningún Administrador entra sin MFA;
- la plantilla genera un `.env` completo.

---

## Fase 2 — Fiabilidad del canal con las estaciones

**Necesidades verificadas:** S-01, S-04, E-01, E-02, E-07, D-1, CR-1, defectos del despliegue del POS.

| # | Trabajo | Dónde |
|---|---|---|
| 2.1 | **Una sola función de publicación MQTT 5**, también para varios mensajes, y que software y despliegues la usen (D-1, E-02) | Servidor: `catalogo`, `software`, `despliegues` |
| 2.2 | **Agente 0.37**, una sola actualización de la flota que incluya: firma obligatoria y validación del catálogo (P0-1); reportes con QoS 1 (E-01); los 4 defectos del despliegue del POS; dejar de suscribirse a los tópicos agregados obsoletos | Agente |
| 2.3 | Enrolamiento sin que la credencial compartida lea las respuestas (P0-2) | Servidor y agente |
| 2.4 | Caducidad de solicitudes de software y despliegues (C-11, E-07) | Servidor |
| 2.5 | `update_fields` en el guardado del latido y de la info de equipo (CR-1) | Servidor |
| 2.6 | Ajustar la ACL para retirar los tópicos agregados | EMQX, **después de 2.2** |

**Dependencias:** 2.2 se distribuye por la actualización por tandas con canario que ya existe.
**Riesgos:**
- Una canaria que falle queda muda hasta recuperarla a mano.
- Las estaciones en 0.29 y 0.32 aceptan firmas inválidas hasta que se actualicen.

**Se acepta cuando:**
- con una ACL quitada a propósito en laboratorio, publicar software o despliegue **informa el
  error**;
- un catálogo sin firma es **rechazado** por 0.37;
- el despliegue del POS en modo "cuando el POS se cierre" se prueba en el piloto con un archivo
  agregado y un rollback;
- toda la flota está en 0.37.

---

## Fase 3 — Escala hacia 1.300 farmacias

**Necesidades verificadas:** E-03, E-04, E-05, puntos únicos de falla (`MAPA_DEPENDENCIAS.md` §6).
**Hay que empezar antes de llegar a unas 900 estaciones**: el techo de EMQX es de 1024 conexiones.

| # | Trabajo | Reutilizar en vez de construir |
|---|---|---|
| 3.1 | Ampliar `max_conns` y `ulimit` de EMQX y declararlos en el compose | Configuración de EMQX |
| 3.2 | Varios procesos del worker MQTT con **suscripciones compartidas** (`$share/...`) | Función nativa de EMQX 5; no requiere una arquitectura nueva |
| 3.3 | Límites de CPU y memoria por contenedor | Compose |
| 3.4 | Sacar el servidor del WiFi; evaluar un segundo host para base y broker | Infraestructura |
| 3.5 | **Prueba de carga** con `simular_agente` antes de cada salto de escala | Comando que ya existe |

**Se acepta cuando** una prueba de carga sostiene el doble de las estaciones previstas para el
siguiente tramo, sin mensajes fallidos y con el latido del worker al día.

---

## Fase 4 — Consolidación del producto

**Necesidades verificadas:** la matriz de consolidación y el alcance de RMM 1.0.

| # | Trabajo | Condición |
|---|---|---|
| 4.1 | Tanda 2 del alcance: ocultar en `/admin/` los modelos de los módulos ocultos; retirar `probar_apertura.py`, la sonda externa y `ActividadPlanificada` | Autorización expresa |
| 4.2 | Llevar `consumir_token` al núcleo; separar lo que monitoreo usa de `whatsapp` | Antes de retirar `aperturas` y `whatsapp` |
| 4.3 | Retirar `viaticos` y `cumplimiento`, exportando sus datos | Que hayan pasado una versión ocultos sin reclamos |
| 4.4 | **Separar inventario y mantenimiento** en otro proyecto que consuma la API RMM (alcance §10) | Respuesta de 0.6 sobre la app móvil y sobre GLPI |
| 4.5 | Decidir si se vuelve a mostrar `scripts` solo para el parcheo de terceros con `winget` | — |

---

## Construir o reutilizar, por área

| Área | Estado verificado | Recomendación |
|---|---|---|
| **Monitoreo de estaciones y conectividad** | Propio, en uso, con conocimiento del negocio que no se compra hecho (POS, nodos, enlaces por proveedor) | **Seguir construyendo**: es el núcleo |
| **Gestión de alertas** | Propia; Telegram funciona, el correo no | Mantener. Reparar el correo o aceptar Telegram como único canal |
| **Despliegues y actualización del POS** | Propio; piloto validado | Mantener. Ninguna herramienta genérica conoce el POS Farmamia |
| **Actualización del agente** | Propia, por tandas y con canario, en uso | Mantener |
| **RMM y administración remota** | MeshCentral (externo) para escritorio; comandos y scripts propios | **Reutilizar** MeshCentral; no construir escritorio remoto |
| **Parches de Windows** | Solo consulta; terceros con `winget` por script | **No construir.** Usar WSUS, Intune o directivas; `winget` para terceros |
| **Gestión de tickets** | No existe integración; la mesa por WhatsApp quedó oculta | **Integrar Odoo Helpdesk** (alerta → ticket) por su API; no construir una mesa propia |
| **ITAM e inventario** | Propio, casi sin datos (22 activos); no hay herramienta externa en la organización (0.6) | **Proyecto separado** que consume la API RMM (fase 4.4) |
| **Mantenimiento en campo** | Propio; la app móvil todavía no se usa (0.6) | Va con inventario al proyecto separado |
| **Indicadores operativos** | Tablero, reportes CSV, resumen de Telegram | Consolidar lo existente antes de agregar un motor de BI |
| **Seguridad y cumplimiento** | Auditoría, MFA, cuatro ojos | Cerrar la fase 1; documentación de gobernanza en `docs/gobernanza/` |
| **Usuarios y organizaciones** | Unidad de negocio y roles en uso | Mantener; MFA obligatorio |
| **Continuidad y recuperación** | Respaldo local sin prueba | Fase 1 (1.2) |
| **Escalabilidad a 1.300** | Techo de EMQX, un host, un worker | Fase 3, con funciones nativas de EMQX |

**No se proponen** microservicios, apps nuevas ni modelos nuevos. Las únicas piezas nuevas
previstas son una función de publicación unificada (2.1) y, si se aprueba, el proyecto separado de
inventario (4.4), que ya tiene su API de integración.
