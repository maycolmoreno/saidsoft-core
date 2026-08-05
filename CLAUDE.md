# Instrucciones para trabajar en saidsoft-core

## Mantener la documentación al día

`README.md` y `PLAN_MODERNIZACION.md` son documentación **viva**, no una foto del
día que se escribieron. Cada vez que se cierra un cambio de alcance real (una fase
nueva, un modelo nuevo, un módulo nuevo, un comando de management nuevo, un cambio de
comportamiento que alguien notaría), actualiza el archivo correspondiente **en el
mismo turno de trabajo**, no como tarea aparte para después:

- **`PLAN_MODERNIZACION.md`**: la fase/feature en la tabla de la sección
  correspondiente (§7 fases originales, §9 fases RMM, §10 auditoría de pendientes).
  Si una fase pendiente de §9/§10 se completa, muévela a "hecho" con una nota breve de
  qué se implementó y en qué archivos vive la lógica principal.
- **`README.md`**: si el cambio afecta cómo alguien arranca el proyecto, qué apps
  existen, o cómo se usa una feature del panel, actualiza la sección correspondiente
  (o agrega una nueva, siguiendo el estilo de las existentes: qué es, por qué, dónde
  vive el código).

Si un docstring en el código queda desactualizado por el cambio (ej. dice "Fase 2,
pendiente" de algo que ya se implementó), corrígelo de una vez — no dejes referencias
a fases futuras que ya pasaron.

Esto surgió porque el 31-jul-2026 ambos documentos quedaron varias fases atrás del
código real (no mencionaban multi-tenancy, alertas, scripts programados, RBAC por
cliente ni reportes por cliente) y hubo que reconstruir el estado a mano. No se debe
repetir.

## Verificación antes de dar por terminado un cambio

- `python manage.py check`
- `python manage.py makemigrations --check --dry-run` (sin drift de migraciones)
- `python manage.py test` (suite completa; el venv está en `.venv/`)
