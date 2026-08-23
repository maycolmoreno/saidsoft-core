# Declaración de Aplicabilidad (SoA) — ISO/IEC 27001:2022, Anexo A

Sistema en alcance: **SAIDSOFT** (repositorio `saidsoft-core`), servidor de producción,
agente RMM y canal MQTT. Base: los 93 controles del Anexo A de ISO/IEC 27001:2022, en
sus cuatro temas (Organizacionales, Personas, Físicos, Tecnológicos).

**Cómo leer esta tabla**: la columna *Aplica* indica si el control corresponde al
alcance de esta organización/sistema. *Estado* describe qué tan implementado está hoy,
con honestidad — "No evaluado" significa exactamente eso, no se disfraza como
cumplimiento. La columna *Referencia* apunta a dónde vive la evidencia real (código,
`PLAN_MODERNIZACION.md`, u otro documento de gobernanza).

Este documento se revisa junto con la matriz de riesgos y la política de seguridad —
ver `docs/gobernanza/matriz-riesgos.md` y `docs/gobernanza/politica-seguridad-informacion.md`.

## A.5 — Controles organizacionales (37)

| # | Control | Aplica | Estado | Referencia |
|---|---|---|---|---|
| 5.1 | Políticas para la seguridad de la información | Sí | Implementado | `politica-seguridad-informacion.md` |
| 5.2 | Roles y responsabilidades de seguridad | Sí | Implementado | `politica-seguridad-informacion.md` §5 |
| 5.3 | Segregación de funciones | Sí | Implementado (regla de cuatro ojos en despliegues y ejecución de scripts a destino amplio) | AC-2, AC-3 |
| 5.4 | Responsabilidades de la dirección | Sí | Implementado (compromiso formal en la política) | `politica-seguridad-informacion.md` §3 |
| 5.5 | Contacto con autoridades | Sí | No evaluado — no hay un procedimiento formal documentado todavía | Pendiente |
| 5.6 | Contacto con grupos de interés especial | Parcial | No evaluado | Pendiente |
| 5.7 | Inteligencia de amenazas | No | No aplica a la escala actual (piloto de 5 estaciones); reevaluar si la operación crece | — |
| 5.8 | Seguridad de la información en la gestión de proyectos | Sí | Implementado de facto: todo cambio sigue el ciclo implementar → probar → documentar → desplegar → verificar | `politica-seguridad-informacion.md` §8 |
| 5.9 | Inventario de activos de información | Parcial | El inventario de activos FÍSICOS de CRESIO (`apps.activos`) es el propio producto; falta un inventario formal de activos de INFORMACIÓN (bases de datos, credenciales, sistemas) como documento de gobernanza aparte | Pendiente |
| 5.10 | Uso aceptable de la información y los activos | Parcial | Implícito en la política; falta un documento de uso aceptable firmado por cada usuario | Pendiente |
| 5.11 | Devolución de activos | Sí | Implementado — `registrar_devolucion` en `apps.activos.services`, con evento auditado | `apps/activos/services.py` |
| 5.12 | Clasificación de la información | No | No implementado — no existe un esquema formal de clasificación (pública/interna/confidencial) | Pendiente |
| 5.13 | Etiquetado de la información | No | No implementado (depende de 5.12) | Pendiente |
| 5.14 | Transferencia de información | Parcial | El canal panel↔agente va cifrado (TLS/HMAC); no hay política formal de transferencia de información entre personas (correo, mensajería) | Pendiente |
| 5.15 | Control de acceso | Sí | Implementado — RBAC por Django Groups/permisos, mínimo privilegio | AC-1, `seed_permisos.py` |
| 5.16 | Gestión de identidades | Sí | Implementado — un usuario Django por persona, sin cuentas compartidas | `politica-seguridad-informacion.md` §6 |
| 5.17 | Información de autenticación | Sí | Implementado — contraseñas ≥12 caracteres, hash estándar de Django, MFA opcional | SEC-4, `config/settings/base.py` |
| 5.18 | Derechos de acceso | Sí | Implementado — permisos por rol, revisables vía `seed_permisos.py` y el admin de Django | AC-1 |
| 5.19 | Seguridad de la información en relaciones con proveedores | Parcial | No hay proveedores de TI externos con acceso al sistema hoy más allá del propio equipo | Pendiente si esto cambia |
| 5.20 | Tratamiento de la seguridad en acuerdos con proveedores | No aplica | Sin proveedores externos con acceso al sistema actualmente | — |
| 5.21 | Gestión de la seguridad en la cadena de suministro TIC | Parcial | Dependencias de software (`requirements.txt`) sin proceso formal de escaneo de vulnerabilidades | Pendiente |
| 5.22 | Supervisión y revisión de servicios de proveedores | No aplica | Sin servicios de proveedores externos gestionando el sistema | — |
| 5.23 | Seguridad de la información en el uso de servicios en la nube | No aplica hoy | El stack corre on-premises (servidor físico); no se usan servicios en la nube todavía — reevaluar si se define un destino de backup en la nube (ver R-17, matriz de riesgos) | OPS-2 |
| 5.24 | Planificación de la gestión de incidentes | Parcial | Existe el mecanismo (bitácora `EventoAuditoria`); falta un procedimiento formal escrito de respuesta a incidentes | Pendiente |
| 5.25 | Evaluación y decisión sobre eventos de seguridad | Parcial | Sin un procedimiento formal; se resuelve caso a caso hoy | Pendiente |
| 5.26 | Respuesta a incidentes de seguridad | Parcial | Igual que 5.24/5.25 — falta formalizar | Pendiente |
| 5.27 | Aprendizaje de incidentes de seguridad | Sí | Implementado de facto — `PLAN_MODERNIZACION.md` documenta cada incidente/hallazgo real y su causa raíz | `PLAN_MODERNIZACION.md` |
| 5.28 | Recolección de evidencia | Sí | Implementado — `EventoAuditoria` conserva usuario, acción, objeto y fecha de cada acción relevante | `apps/auditoria` |
| 5.29 | Seguridad de la información durante la disrupción | Parcial | Backups + procedimiento de restauración probado; sin plan formal de continuidad de negocio más amplio | OPS-1, OPS-2 |
| 5.30 | Preparación TIC para la continuidad del negocio | Parcial | Backups cifrados con restore probado; sin sitio de respaldo/failover | OPS-2, R-21 |
| 5.31 | Requisitos legales, estatutarios, regulatorios y contractuales | No evaluado | Fuera del alcance de esta auditoría técnica — requiere revisión legal/normativa propia | Pendiente |
| 5.32 | Derechos de propiedad intelectual | No evaluado | Fuera del alcance de esta auditoría | Pendiente |
| 5.33 | Protección de los registros | Sí | Implementado — backups cifrados, `EventoAuditoria` como registro append-only en la práctica | OPS-2 |
| 5.34 | Privacidad y protección de datos personales | Parcial | Se manejan datos personales de colaboradores (`apps.activos.Colaborador`) y credenciales de bitlocker cifradas; sin un análisis formal de protección de datos personales (DPIA) | Pendiente |
| 5.35 | Revisión independiente de la seguridad de la información | Sí | Esta auditoría (22-ago-2026) es la primera revisión formal — se recomienda repetirla periódicamente, idealmente con un tercero | `PLAN_MODERNIZACION.md` |
| 5.36 | Cumplimiento de políticas, reglas y normas de seguridad | Parcial | Sin un proceso formal de verificación periódica de cumplimiento (más allá de esta auditoría puntual) | Pendiente |
| 5.37 | Procedimientos operativos documentados | Sí | Implementado — `deploy/README-produccion.md`, `deploy/backup.sh`/`restaurar-backup.sh`, `agente-prueba/README.md` | `deploy/` |

## A.6 — Controles de personas (8)

| # | Control | Aplica | Estado | Referencia |
|---|---|---|---|---|
| 6.1 | Verificación de antecedentes | No evaluado | Fuera del alcance de esta auditoría técnica — es un proceso de RRHH | Pendiente |
| 6.2 | Términos y condiciones de empleo | No evaluado | Fuera del alcance — RRHH | Pendiente |
| 6.3 | Concientización, educación y capacitación en seguridad | No | No implementado — no hay un programa de capacitación formal | Pendiente |
| 6.4 | Proceso disciplinario | Parcial | Mencionado en la política (§3); sin procedimiento formal separado | `politica-seguridad-informacion.md` §3 |
| 6.5 | Responsabilidades tras la finalización o cambio de empleo | No | No implementado — falta un procedimiento formal de baja de accesos | Pendiente (riesgo: cuentas de ex-colaboradores sin desactivar) |
| 6.6 | Acuerdos de confidencialidad o no divulgación | No evaluado | Fuera del alcance — RRHH/legal | Pendiente |
| 6.7 | Trabajo remoto | Parcial | El panel es accesible remotamente por diseño (HTTPS); sin lineamientos formales de trabajo remoto seguro (VPN, dispositivos personales) | Pendiente |
| 6.8 | Reporte de eventos de seguridad de la información | Sí | Implementado — canal descrito en la política (§10); mecanismo técnico ya existente (`EventoAuditoria`) | `politica-seguridad-informacion.md` §10 |

## A.7 — Controles físicos (14)

El servidor de producción es un equipo físico en sitio (Intel NUC, hostname
`glpi-NUC11TNKv5`) — **ningún control de este bloque fue evaluado en esta auditoría**,
que fue exclusivamente técnica/lógica (código, configuración, infraestructura de
software). Se listan igual por completitud del SoA, marcados explícitamente como
pendientes de una revisión de sitio.

| # | Control | Aplica | Estado | Referencia |
|---|---|---|---|---|
| 7.1 | Perímetros de seguridad física | Sí | No evaluado — requiere visita de sitio | R-20, matriz de riesgos |
| 7.2 | Entrada física | Sí | No evaluado | R-20 |
| 7.3 | Seguridad de oficinas, salas e instalaciones | Sí | No evaluado | R-20 |
| 7.4 | Monitoreo de seguridad física | Sí | No evaluado | R-20 |
| 7.5 | Protección contra amenazas físicas y ambientales | Sí | No evaluado | R-20 |
| 7.6 | Trabajo en áreas seguras | Sí | No evaluado | R-20 |
| 7.7 | Escritorio y pantalla limpios | Sí | No evaluado (política organizacional, no técnica) | Pendiente |
| 7.8 | Ubicación y protección de equipos | Sí | No evaluado | R-20 |
| 7.9 | Seguridad de activos fuera de las instalaciones | Sí | Parcialmente aplicable — las estaciones del piloto están en las farmacias, fuera del sitio del servidor; sin control físico sobre ellas más allá del control lógico (agente + firma de comandos) | SEC-1 |
| 7.10 | Medios de almacenamiento | Sí | No evaluado (discos, backups en medios físicos) | Pendiente |
| 7.11 | Servicios de suministro | Sí | No evaluado (energía, climatización del sitio del servidor) | R-20 |
| 7.12 | Seguridad del cableado | Sí | No evaluado | R-20 |
| 7.13 | Mantenimiento de equipos | Sí | No evaluado | R-20 |
| 7.14 | Eliminación o reutilización segura de equipos | Sí | Parcialmente aplicable a nivel lógico: `registrar_baja` distingue motivos (destrucción, donación) pero no hay procedimiento de borrado seguro de disco documentado | `apps/activos/models.py` (MotivoBaja) |

## A.8 — Controles tecnológicos (34)

| # | Control | Aplica | Estado | Referencia |
|---|---|---|---|---|
| 8.1 | Dispositivos de punto final del usuario | Parcial | Cubre las estaciones de farmacia (agente RMM); sin política de dispositivos personales/BYOD para el panel | SEC-1 |
| 8.2 | Derechos de acceso privilegiado | Sí | Implementado — superusuarios limitados a 2 cuentas reales, resto por rol | AC-1 |
| 8.3 | Restricción de acceso a la información | Sí | Implementado — scoping por unidad de negocio (tenant) en todo el panel | AC-1, `apps.cuentas.services` |
| 8.4 | Acceso al código fuente | Sí | Implementado — repositorio Git con control de acceso propio (GitHub) | — |
| 8.5 | Autenticación segura | Sí | Implementado — contraseñas + bloqueo por fuerza bruta + MFA opcional | SEC-3, SEC-4 |
| 8.6 | Gestión de capacidad | Parcial | Sin monitoreo formal de capacidad del servidor (CPU/disco/memoria) más allá de lo operativo | Pendiente |
| 8.7 | Protección contra malware | No evaluado | Depende del antivirus/EDR de cada estación y del servidor — no evaluado en esta auditoría | Pendiente |
| 8.8 | Gestión de vulnerabilidades técnicas | Parcial | Sin proceso formal de escaneo periódico de vulnerabilidades (dependencias, SO, contenedores) | Pendiente |
| 8.9 | Gestión de la configuración | Sí | Implementado — Docker Compose versionado, `.env.prod.example` documentado, configuración como código | `deploy/` |
| 8.10 | Eliminación de información | Parcial | El kardex de inventario nunca borra filas (por diseño, trazabilidad); sin política de retención/purga de datos viejos más allá de `purgar_metricas` (métricas de monitoreo) | `apps/monitoreo` |
| 8.11 | Enmascaramiento de datos | Parcial | Las claves de recuperación de BitLocker se cifran en reposo (Fernet) y solo se muestran bajo demanda con permiso y auditoría | `apps/catalogo/crypto.py` |
| 8.12 | Prevención de fuga de datos | No | No implementado — sin DLP formal | Pendiente |
| 8.13 | Respaldo de la información | Sí | Implementado — backup cifrado nocturno, restauración probada | OPS-1, OPS-2 |
| 8.14 | Redundancia de instalaciones de procesamiento de información | No | Sin redundancia — un único servidor. Proporcional al tamaño actual del piloto | R-21 |
| 8.15 | Registro (logging) | Sí | Implementado — `EventoAuditoria`, logs de aplicación, logs del agente | `apps/auditoria` |
| 8.16 | Actividades de monitoreo | Sí | Implementado — monitoreo de estaciones (CPU/RAM/disco/red), alertas configurables | `apps.monitoreo` |
| 8.17 | Sincronización de reloj | Parcial | La firma de comandos depende de una ventana de tiempo (120s) — implica que servidor y agentes deben tener el reloj razonablemente sincronizado; sin verificación explícita de NTP documentada | SEC-1 |
| 8.18 | Uso de programas de utilidad privilegiados | Parcial | Acceso a `manage.py shell`/Django admin limitado a superusuarios | AC-1 |
| 8.19 | Instalación de software en sistemas operativos | Sí | Implementado — instalación de software a las estaciones vía el canal firmado y auditado del panel (catálogo de software), no manual | `apps.software` |
| 8.20 | Seguridad de redes | Parcial | TLS en MQTT y HTTPS en el panel; sin segmentación de red documentada más allá de lo que provee el propio EMQX/Docker | SEC-1, SEC-2 |
| 8.21 | Seguridad de los servicios de red | Sí | Implementado — ACLs de EMQX por estación (parcialmente migrado, ver R-19) | `apps/mqtt_worker/emqx_admin.py` |
| 8.22 | Segregación de redes | Parcial | Los servicios corren en contenedores Docker separados con su propia red interna; sin segmentación de red física más amplia evaluada | `deploy/docker-compose.yml` |
| 8.23 | Filtrado web | No aplica | No es un control relevante para este sistema (no hay navegación de usuarios finales a través del sistema) | — |
| 8.24 | Uso de criptografía | Sí | Implementado — HTTPS, TLS de MQTT, HMAC-SHA256 en comandos, GPG/AES256 en backups, Fernet en claves de BitLocker | SEC-1, SEC-2, OPS-2 |
| 8.25 | Ciclo de vida de desarrollo seguro | Parcial | Ciclo implementar→probar→documentar→desplegar es consistente, pero no está formalizado como política de SDLC separada | `politica-seguridad-informacion.md` §8 |
| 8.26 | Requisitos de seguridad de las aplicaciones | Sí | Implementado de facto — cada hallazgo de esta auditoría se trató como un requisito de seguridad a nivel de código | `PLAN_MODERNIZACION.md` |
| 8.27 | Arquitectura y principios de ingeniería de sistemas seguros | Sí | Implementado — separación servicios/vistas, mínimo privilegio, defensa en profundidad (ej. verificación de estación en la firma, redundante con la ACL del tópico) | SEC-1 |
| 8.28 | Codificación segura | Sí | Implementado — uso de ORM (previene inyección SQL), `hmac.compare_digest` (previene timing attacks), consultas parametrizadas | Todo el código de `apps/` |
| 8.29 | Pruebas de seguridad en desarrollo y aceptación | Sí | Implementado — suite de tests que cubre explícitamente casos de seguridad (permisos, bloqueo, firmas, expiración de sesión) | Cientos de tests en `apps/*/tests.py` |
| 8.30 | Desarrollo subcontratado | No aplica | Sin desarrollo subcontratado | — |
| 8.31 | Separación de entornos de desarrollo, prueba y producción | Sí | Implementado — settings separados (`desarrollo.py`/`produccion.py`), base de datos de test aislada | `config/settings/` |
| 8.32 | Gestión de cambios | Sí | Implementado — Git como control de versiones, cada cambio documentado y probado antes de desplegar | `politica-seguridad-informacion.md` §8 |
| 8.33 | Información de prueba | Parcial | Los tests usan datos ficticios (`seed_demo`, fixtures); sin política formal de qué datos NUNCA deben usarse en pruebas | `apps/*/tests.py` |
| 8.34 | Protección de los sistemas de información durante las pruebas de auditoría | Sí | Implementado en esta misma auditoría — las pruebas de restauración de backup corrieron contra una base `restore_test` separada, nunca contra la base real sin confirmación explícita | OPS-2, `deploy/restaurar-backup.sh` |

## Resumen

| Tema | Controles | Implementados (Sí) | Parciales | No aplica | No evaluados/pendientes |
|---|---|---|---|---|---|
| A.5 Organizacionales | 37 | 15 | 12 | 2 | 8 |
| A.6 Personas | 8 | 2 | 3 | 0 | 3 |
| A.7 Físicos | 14 | 0 | 1 | 0 | 13 |
| A.8 Tecnológicos | 34 | 18 | 12 | 2 | 2 |
| **Total** | **93** | **35** | **28** | **4** | **26** |

El bloque más débil es, con claridad, **A.7 (Físicos)** — ninguno de sus controles fue
evaluado, porque esta auditoría fue exclusivamente técnica sobre el código y la
infraestructura de software. Una revisión de sitio del servidor físico es la
recomendación de mayor impacto que queda fuera de este documento.
