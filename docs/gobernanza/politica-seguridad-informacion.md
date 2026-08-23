# Política de Seguridad de la Información — CRESIO

| | |
|---|---|
| **Organización** | CRESIO (Farmacias San Gregorio, MIA, 7DIAS) |
| **Alcance** | Toda la organización; sistema principal en alcance: **SAIDSOFT** (plataforma de gestión de activos, RMM y despliegues — repositorio `saidsoft-core`) |
| **Versión** | 1.0 |
| **Fecha de emisión** | 22-ago-2026 |
| **Próxima revisión** | 22-ago-2027 (o antes, ante un incidente significativo o un cambio mayor de infraestructura) |
| **Referencia técnica** | `PLAN_MODERNIZACION.md` (bitácora de incidentes y cambios), `docs/gobernanza/matriz-riesgos.md`, `docs/gobernanza/soa.md` |

## 1. Objeto

Esta política establece los principios, roles y reglas mínimas para proteger la
confidencialidad, integridad y disponibilidad de la información de CRESIO — en
particular la que administra y transporta SAIDSOFT: datos de las tres unidades de
negocio (San Gregorio, MIA, 7DIAS), inventario de activos, credenciales de acceso
remoto, y los canales de control (MQTT/HMAC) hacia las estaciones de las farmacias.

## 2. Alcance

Aplica a toda persona con acceso a sistemas de información de CRESIO: personal propio,
contratistas y proveedores de soporte técnico. El sistema técnico auditado en
profundidad bajo esta política es **SAIDSOFT** (servidor de producción, agente RMM en
las estaciones, y el canal MQTT que los conecta). Otros sistemas de la organización
(facturación, POS, redes de farmacia) quedan dentro del alcance de la política pero no
fueron objeto de la auditoría técnica de 22-ago-2026 que originó este documento — su
cumplimiento queda pendiente de una revisión propia.

## 3. Compromiso de la dirección

La dirección de CRESIO respalda esta política, provee los recursos necesarios para
mantenerla vigente, y espera que cada responsable de área haga cumplir sus reglas
dentro de su equipo. El incumplimiento de esta política puede derivar en medidas
disciplinarias, sin perjuicio de responsabilidades legales que correspondan.

## 4. Principios

- **Confidencialidad**: la información se comparte solo con quien la necesita para su
  función (mínimo privilegio) — ver §6.
- **Integridad**: los cambios a datos críticos (stock, inventario, configuración de
  estaciones) quedan trazados y son reversibles cuando es razonable (ver §8, gestión de
  cambios; y el kardex de `MovimientoInventario`, que nunca borra una fila, solo suma
  el movimiento inverso).
- **Disponibilidad**: los sistemas críticos tienen respaldo (ver §9) y un plan de
  continuidad proporcional a su tamaño real — hoy un piloto de 5 estaciones, no una
  operación a escala completa.
- **Mejora continua**: la seguridad se revisa y corrige de forma iterativa, con
  evidencia real (pruebas, corridas contra el sistema de producción) antes de darla
  por cerrada — no se documenta un control como implementado sin haberlo verificado.

## 5. Roles y responsabilidades

| Rol | Responsabilidad |
|---|---|
| **Dirección de CRESIO** | Aprueba esta política y sus revisiones; asigna recursos. |
| **Responsable técnico de SAIDSOFT** | Mantiene el sistema, aplica esta política en el código y la infraestructura, es dueño de la matriz de riesgos y el SoA. |
| **Administradores del panel** (`Administrador`, Django `is_superuser`) | Acceso total; deben ser las cuentas menos numerosas posible (hoy: 2 personas). |
| **Soporte Técnico / Operador RMM / Mesa de Ayuda** (grupos definidos en `apps/activos/management/commands/seed_permisos.py`) | Acceso acotado a su función — nunca acceso total por defecto. |
| **Cualquier usuario del panel** | Responsable de proteger su propia credencial, activar la verificación en dos pasos si su rol maneja acciones de riesgo, y reportar cualquier incidente sospechoso. |

## 6. Control de acceso

- Acceso por **rol** (Groups + permisos estándar de Django), nunca por cuenta
  compartida — cada persona tiene su propio usuario, cada acción queda atribuida
  (`apps.auditoria.EventoAuditoria`).
- **Mínimo privilegio**: un rol solo recibe los permisos que su función requiere.
  Ejemplo vigente: Mesa de Ayuda puede consultar información de una estación pero no
  reiniciarla ni aprobarla; esa distinción es un control activo, no aspiracional (ver
  `EstacionMesaDeAyudaVsSoporteTecnicoTests`).
- **Contraseñas**: mínimo 12 caracteres (`MinimumLengthValidator`), más los validadores
  estándar de Django (no numérica, no común).
- **Bloqueo por fuerza bruta**: 5 intentos fallidos bloquean la cuenta por 1 hora
  (`django-axes`, bloqueo por usuario, no por IP — varias estaciones de una misma
  farmacia comparten salida a internet por NAT).
- **Verificación en dos pasos (MFA)**: disponible para cualquier usuario (TOTP,
  `/cuenta/mfa/`). **No es obligatoria todavía** para ningún rol — es una decisión
  deliberada para no bloquear operación durante la adopción inicial, sujeta a
  revisión: se recomienda evaluar volverla obligatoria para el grupo Administrador en
  la próxima revisión de esta política (ver matriz de riesgos, ítem de MFA).
- **Sesiones**: expiran tras 8 horas de inactividad y se cierran solas al cerrar el
  navegador.
- **Cuatro ojos** en acciones de alto impacto sobre toda la flota: un despliegue o una
  ejecución de script a destino amplio (cadena/grupos/farmacias) requiere que un
  segundo usuario, distinto de quien lo creó, lo apruebe explícitamente.

## 7. Seguridad de las comunicaciones

- El panel se sirve únicamente por HTTPS; las cookies de sesión llevan el flag
  `Secure`.
- Los comandos hacia las estaciones (reiniciar, ejecutar script, desplegar software)
  van firmados (HMAC-SHA256) y atados a la estación destino y a una ventana de tiempo
  corta, para que un mensaje capturado no pueda reproducirse más tarde ni reenviarse a
  otra estación.
- El canal MQTT usa TLS de punta a punta en producción.
- **Riesgo residual aceptado y documentado** (no resuelto por diseño, ver matriz de
  riesgos): la clave de firma de comandos es única para toda la flota, no por
  estación — un agente comprometido podría, en teoría, fabricar un comando válido
  "para otra estación". Cerrarlo requeriría una clave por estación, un cambio mayor
  pendiente de priorizar.

## 8. Gestión de cambios

Todo cambio a SAIDSOFT sigue el mismo ciclo, sin excepciones para cambios de
seguridad: implementar → probar (suite automatizada + verificación manual cuando
aplica) → documentar en `PLAN_MODERNIZACION.md` con evidencia real → revisar →
desplegar → verificar en producción. Los cambios que alteran el protocolo entre el
servidor y el agente de las estaciones (como una firma de comandos) se secuencian con
cuidado especial: primero se actualiza el agente, después el servidor, nunca al revés,
para no dejar estaciones reales sin capacidad de recibir comandos.

## 9. Continuidad y respaldo

- Copia de la base de datos y de los archivos de media todas las noches, cifrada
  (GPG/AES256) y con una retención de 14 días en el propio servidor.
- La capacidad de restaurar un respaldo se prueba (no se asume) — ver
  `deploy/restaurar-backup.sh` y su corrida real documentada en `PLAN_MODERNIZACION.md`
  (ítem OPS-2).
- **Pendiente**: copia de los respaldos fuera del servidor. Es un hallazgo abierto,
  sujeto a que la organización defina un destino (otro servidor propio, o
  almacenamiento en la nube) — ver matriz de riesgos.
- No existe hoy un sitio de respaldo/failover — el stack corre en un único servidor
  físico en sitio. Proporcional al tamaño actual del piloto; a reevaluar si la
  operación crece.

## 10. Gestión de incidentes

Cualquier persona que detecte un incidente de seguridad (acceso no autorizado, pérdida
de un dispositivo con acceso al panel, sospecha de credenciales comprometidas) debe
notificarlo de inmediato al responsable técnico de SAIDSOFT. Los eventos relevantes del
sistema (login, aprobaciones, cambios de configuración, acciones de riesgo sobre una
estación) ya quedan registrados en `EventoAuditoria` con usuario, fecha y detalle —
esa bitácora es la primera fuente a revisar ante cualquier incidente.

## 11. Cumplimiento

Esta política se alinea con el Anexo A de ISO/IEC 27001:2022. El detalle
control-por-control de qué aplica, qué está implementado y con qué evidencia, vive en
`docs/gobernanza/soa.md` (Declaración de Aplicabilidad). Los riesgos identificados y su
tratamiento viven en `docs/gobernanza/matriz-riesgos.md`.

## 12. Revisión

Esta política se revisa al menos una vez al año, o antes si ocurre un incidente
significativo, un cambio mayor de infraestructura, o una nueva auditoría. Cada
revisión debe actualizar la versión y la fecha en el encabezado de este documento.
