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

Se conservaron las consultas `docs/auditoria-*.sql`: son herramientas de verificación que
se pueden volver a correr (el runbook de `deploy/README-produccion.md` usa
`auditoria-fase5-verificacion.sql`).
