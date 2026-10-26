# Documentos retirados

El 22-oct-2026 se quitaron del repositorio los documentos que describían el sistema en un
momento ya superado (auditorías, diagnósticos y propuestas de septiembre y octubre de
2026). Seguir teniéndolos a la vista confundía: sus cifras y conclusiones ya no
coincidían con el código.

**No se perdieron.** Siguen en el historial de git. Todos existen tal cual en el commit
`72d01f1`:

```
git show 72d01f1:docs/auditoria-snmp.md
git show 72d01f1:docs/auditoria/centro-monitoreo/04-riesgos-del-encargo.md
```

Comentarios del código, `PLAN_MODERNIZACION.md` y algunos documentos todavía los citan
como origen de una decisión: esas citas siguen siendo válidas, se leen con el comando de
arriba.

| Documento | Fecha | Qué era |
|---|---|---|
| `docs/auditoria/fase4-hallazgos.md` | 10-oct-2026 | Hallazgos de la auditoría por agentes (94 únicos) |
| `docs/auditoria/fase5-triage.md` | 12-oct-2026 | Triage de los hallazgos ALTO |
| `docs/auditoria/noc-panel-principal.md` | 13-oct-2026 | Auditoría del panel principal |
| `docs/auditoria/centro-monitoreo/` (14 archivos) | 11-oct-2026 | Auditoría del tablero de monitoreo (hallazgos D-01 a D-06) |
| `docs/auditoria-snmp.md` | 8-oct-2026 | Auditoría y plan por fases del monitoreo SNMP |
| `docs/preparacion-inventario.md` | 7-oct-2026 | Cambio mínimo del inventario para monitoreo |
| `docs/auditoria-experiencia-tecnico.md` | 3-oct-2026 | Experiencia del técnico de soporte |
| `docs/auditoria-capacidades.md` | 27-sep-2026 | Qué datos maneja la plataforma |
| `docs/capacidad-2030.md` | 27-sep-2026 | ¿Aguanta 1.300 farmacias? |
| `docs/prioridad-2030.md` | 27-sep-2026 | Matriz de prioridad 2030 |
| `docs/tres-agendas.md` | 26-sep-2026 | Mantenimiento / ActividadPlanificada / VisitaTecnica |
| `docs/auditoria-arquitectura.md` | 12-sep-2026 | Auditoría de arquitectura del panel |
| `docs/evaluacion-cresio-enlaces.md` | 11-sep-2026 | Evaluación del sistema anterior de enlaces |

## Segunda tanda (26-oct-2026)

Mismo criterio: fotos de un momento ya superado. Existen tal cual en el commit `9c9405b`
(`git show 9c9405b:<ruta>`).

| Documento | Fecha | Qué era |
|---|---|---|
| `docs/consolidacion/AUDITORIA_FLUJO_ESTACION.md` | 20-oct-2026 | Recorrido de una estación, de enrolamiento a confirmaciones |
| `docs/consolidacion/AUDITORIA_SEGURIDAD_ESTABILIDAD.md` | 20-oct-2026 | Estado de seguridad y estabilidad (S-xx, E-xx) |
| `docs/consolidacion/INVENTARIO_TECNICO.md` | 20-oct-2026 | Foto de lo que existía en el commit `68d0045` |
| `docs/consolidacion/MAPA_DEPENDENCIAS.md` | 20-oct-2026 | Dependencias entre apps y puntos únicos de falla |
| `docs/consolidacion/MATRIZ_CONSOLIDACION.md` | 20-oct-2026 | Qué conservar, consolidar o retirar |
| `docs/consolidacion/ROADMAP_SAIDSOFT.md` | 20-oct-2026 | Roadmap por fases (lo reemplaza el plan estratégico 2027) |
| `docs/consolidacion/GUIA_STAGING_COLA.md` | 21-oct-2026 | Validación de la cola en staging (no se armó staging; se fue al piloto) |
| `docs/entrega-proyecto/` (3 archivos) | 27-ago-2026 | Casos de uso, diagrama de clases y manual de usuario de la entrega del proyecto |
| `docs/proyecto-despliegue-pos.md` | 5-oct-2026 | Propuesta a dirección: actualización del POS en la cadena |
| `docs/requerimiento-despliegue-pos.md` | 5-oct-2026 | Requerimiento a dirección: control de versiones del POS |

Se conservaron las consultas `docs/auditoria-*.sql`: son herramientas de verificación que
se pueden volver a correr (el runbook de `deploy/README-produccion.md` usa
`auditoria-fase5-verificacion.sql`).
