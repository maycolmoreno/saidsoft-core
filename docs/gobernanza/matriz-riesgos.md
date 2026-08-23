# Matriz de riesgos — SAIDSOFT

Inventario de riesgos identificados en la auditoría de gobernanza del 22-ago-2026
(ver `PLAN_MODERNIZACION.md`, sección "Auditoría de pendientes") y su estado actual.
Escala de probabilidad e impacto: **Baja / Media / Alta**. Nivel de riesgo = combinación
cualitativa de ambas. Un riesgo "Cerrado" significa que el tratamiento ya se implementó
y se verificó (con tests y, cuando aplicaba, contra el servidor real) — no que el
riesgo residual sea cero; ver la columna "Riesgo residual" cuando corresponda.

Esta matriz es un documento vivo: se actualiza cada vez que se cierra, se descubre o se
reprioriza un riesgo. La fuente de verdad detallada de cada fix es
`PLAN_MODERNIZACION.md`; acá se resume para gestión.

## Riesgos cerrados

| ID | Activo / Proceso | Amenaza o vulnerabilidad | Prob. | Impacto | Nivel | Tratamiento | Estado | Ref. |
|---|---|---|---|---|---|---|---|---|
| R-01 | Backups | El cron de respaldo nunca corría — cero backups reales existían | Alta | Alta | **Crítico** | Cron instalado y verificado con corrida real | Cerrado 22-ago-2026 | OPS-1 |
| R-02 | Panel web | Servido por HTTP plano, credenciales y sesión viajaban sin cifrar | Media | Alta | **Alto** | HTTPS obligatorio, cookies `Secure`, excepción documentada para `/media/` (agentes sin CA configurada) | Cerrado 22-ago-2026 | SEC-2 |
| R-03 | Vistas del panel | ~85% de las vistas solo pedían sesión iniciada, sin permiso por rol | Alta | Alta | **Crítico** | `@permission_required` por vista, permisos estándar de Django, roles en `seed_permisos.py` | Cerrado 22-ago-2026 (con corrección posterior, ver R-03b) | AC-1 |
| R-03b | Vistas del panel (`scripts.py`/`estaciones.py`/`monitoreo.py`) | El cierre de AC-1 se saltó 15 vistas de tres módulos sin revisarlas | Media | Alta | **Alto** | Barrido completo del panel confirmando cero vistas sin permiso restantes | Cerrado 22-ago-2026 | AC-1 (corrección) |
| R-04 | Aprobación de despliegues | La regla de "cuatro ojos" no exigía ningún permiso — cualquier segundo usuario contaba | Media | Alta | **Alto** | Permiso `aprobar_despliegue` explícito, no otorgado a ningún rol operativo por defecto | Cerrado 22-ago-2026 | AC-2 |
| R-05 | Ejecución de scripts a destino amplio | Sin aprobación de un segundo usuario para correr código en toda la cadena | Media | Alta | **Alto** | Mismo esquema de cuatro ojos que despliegues (`aprobar_ejecucionscript`) | Cerrado 22-ago-2026 | AC-3 |
| R-06 | Login del panel | Fuerza bruta ilimitada contra `/login/` y `/admin/` | Alta | Alta | **Crítico** | `django-axes`, bloqueo por usuario tras 5 intentos | Cerrado 22-ago-2026 | SEC-3 |
| R-07 | Stock de bodega | Condición de carrera: dos salidas/entregas simultáneas podían perder una actualización | Baja | Media | Medio | `select_for_update` + `F()` en vez de leer-modificar-escribir en Python | Cerrado 22-ago-2026 | BUG-1 |
| R-08 | Ciclo de vida de `Activo` | Transiciones sin `transaction.atomic` — un fallo a mitad de camino dejaba estado sin historial | Baja | Media | Medio | Las 9 funciones de transición envueltas en `@transaction.atomic` | Cerrado 22-ago-2026 | BUG-2 |
| R-09 | Firma de comandos MQTT | La firma de un comando sin parámetros era un string constante — reproducible indefinidamente | Media | Alta | **Alto** | `estación`+`timestamp` entran a la firma; ventana de 120s | Cerrado (código), **desplegado parcialmente** — ver R-09b | SEC-1 |
| R-09b | Mensajes de Despliegue/SolicitudInstalacion | No tenían firma alguna (peor que R-09) — solo la ACL del broker los protegía | Media | **Crítica** | **Crítico** | Mismo esquema de firma que R-09 | Cerrado (código), desplegado | SEC-1 |
| R-10 | Rollout del fix de firma (R-09/R-09b) | El servidor y el agente deben coincidir en el esquema de firma — desplegar uno sin el otro rompe el control remoto de las estaciones no actualizadas | Alta (si se hace sin cuidado) | Media (operativo, no de seguridad) | Medio | Secuencia documentada: agente primero, servidor después; mecanismo de actualización remota construido para futuros rollouts | Cerrado el procedimiento; **rollout real en curso**, ver "Riesgos abiertos" | SEC-1 / actualización de agente |
| R-11 | Herramienta de desarrollo `simular_agente.py` | No respetaba `MQTT_USE_TLS` — se conectaba siempre en plano | Baja (solo dev) | Baja | Bajo | Mismo chequeo de TLS que el resto del código | Cerrado 22-ago-2026 | SEC-6 |
| R-12 | Sesiones del panel | Sin expiración configurada — el default de Django (2 semanas, sobrevive cierre del navegador) | Media | Media | Medio | 8 horas de inactividad + cierre al cerrar el navegador | Cerrado 22-ago-2026 | SEC-5 |
| R-13 | Autenticación del panel | Sin segundo factor disponible en ninguna cuenta | Media | Alta | **Alto** | MFA (TOTP) disponible, opcional por usuario | Cerrado parcialmente — ver R-13b en riesgos abiertos | SEC-4 |
| R-14 | Backups | Quedaban sin cifrar en el propio servidor | Media | Alta | **Alto** | Cifrado GPG/AES256, script se niega a correr sin la passphrase | Cerrado 22-ago-2026 | OPS-2 |
| R-15 | Backups | Nunca se había probado restaurar uno real (solo integridad del archivo) | Media | **Crítica** | **Crítico** | Restauración real ejecutada y verificada tabla por tabla y fila por fila contra la base real | Cerrado 22-ago-2026 | OPS-2 |
| R-16 | `Activo`/`MovimientoInventario`/`RecepcionLote` | Estados declarados (`EN_TRANSITO`, `AJUSTE`, `ANULADO`) sin funcionalidad real detrás | Baja | Baja | Bajo | `AJUSTE`/`ANULADO` implementados; `EN_TRANSITO` eliminado (no correspondía al dominio) | Cerrado 22-ago-2026 | BUG-3 |

## Riesgos abiertos

| ID | Activo / Proceso | Amenaza o vulnerabilidad | Prob. | Impacto | Nivel | Tratamiento propuesto | Bloqueante | Ref. |
|---|---|---|---|---|---|---|---|---|
| R-10b | Estaciones del piloto (5 en total) | Solo 1 de 5 estaciones tiene el agente actualizado al esquema de firma nuevo — las otras 4 rechazan comandos remotos (no es una brecha de seguridad, es una degradación funcional temporal) | Alta (mientras no se actualicen) | Media | Medio | Repetir en las 4 restantes el mismo procedimiento manual verificado en ML006-A (hash, detener, copiar, arrancar) | Decisión operativa de cuándo hacerlo — sin fecha definida | SEC-1 |
| R-13b | Cuentas de Administrador (máximo privilegio) | MFA existe pero no es obligatorio para ningún rol, incluido Administrador | Media | Alta | **Alto** | Evaluar volver el MFA obligatorio para el grupo Administrador en la próxima revisión de la política | Decisión de gobernanza, no técnica | SEC-4 |
| R-17 | Backups | Sin copia fuera del servidor — un incendio, robo o falla de disco del único servidor físico deja a CRESIO sin datos ni respaldo | Baja | **Crítica** | **Alto** | Definir destino (otro servidor propio por SSH/rsync, o almacenamiento en la nube) — el servidor ya tiene `rsync`/`gpg` instalados y salida a internet confirmada | **Sí — depende de que la organización defina el destino** | OPS-2 |
| R-18 | Firma de comandos MQTT | La clave de firma (`COMANDO_HMAC_SECRET`) es única para toda la flota, no por estación — un agente comprometido podría fabricar un comando válido "para otra estación" | Baja | Media | Medio | Requeriría clave HMAC por estación (mismo patrón que ya existe para credenciales MQTT por estación) — cambio mayor, no trivial | No — riesgo aceptado por ahora, documentado | SEC-1 |
| R-19 | Credencial MQTT compartida (`MQTT_USERNAME_AGENTE`) | Mientras la migración a credenciales por estación no se complete, esa credencial compartida sigue con ACL `/saidsof/#` (acceso a todo) | Baja | Media | Medio | Correr `deploy/emqx-narrow-acl-agente.sh` una vez confirmado que toda la flota migró a credencial propia | No — depende de completar la migración de agentes | SEC-1 (hallazgo relacionado) |
| R-20 | Servidor físico (Intel NUC, `glpi-NUC11TNKv5`) | Seguridad física del sitio (acceso al equipo, control ambiental) no evaluada en esta auditoría — es una auditoría de aplicación e infraestructura lógica, no una visita de sitio | Desconocida | Alta | **No evaluado** | Programar una revisión de seguridad física como ítem propio | Sí, si se busca cumplimiento formal de A.7 (Anexo A) | GOV-1 / SoA |
| R-21 | Disponibilidad del stack | Un único servidor físico sin sitio de respaldo/failover documentado | Baja (hoy, piloto pequeño) | Alta | Medio | Reevaluar si la operación crece más allá del piloto actual | No, proporcional al tamaño actual | GOV-1 |
| R-22 | Gobernanza documental | Ausencia de política formal de seguridad, matriz de riesgos y SoA hasta esta fecha | — | — | — | Este mismo conjunto de documentos | Cerrado 22-ago-2026 (este documento) | GOV-1 |

## Cómo se mantiene esta matriz

- Cada vez que `PLAN_MODERNIZACION.md` registra el cierre de un hallazgo de seguridad,
  gobernanza u operaciones, se refleja acá con su ID y referencia cruzada.
- Un riesgo "cerrado" que reabre (por una regresión, o porque cambian las
  circunstancias — ej. la flota de estaciones crece) vuelve a la tabla de abiertos con
  una nota explicando por qué se reabrió.
- Revisión mínima: junto con cada revisión anual de la política de seguridad
  (`politica-seguridad-informacion.md`), o antes si se cierra/descubre un riesgo con
  nivel **Alto** o **Crítico**.
