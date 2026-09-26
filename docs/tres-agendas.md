# Mantenimiento, ActividadPlanificada y VisitaTecnica: ¿se fusionan?

**Fecha:** 26-sep-2026
**Alcance:** solo investigación. No se tocó código.
**Origen:** [`modulos.md`](modulos.md) marcó los tres como "el duplicado real del
proyecto" y recomendó que `Mantenimiento` absorbiera a `VisitaTecnica`.

> **La recomendación anterior estaba equivocada, y este documento la corrige.** Se hizo
> mirando nombres de pantalla y modelos por encima; al abrir el código aparecieron dos
> hechos que la invalidan.

---

## Los dos hechos que cambian todo

### 1. `Mantenimiento` y `VisitaTecnica` ya están relacionados

```python
# apps/mantenimiento/models.py:162
visita = models.ForeignKey(
    'mantenimiento.VisitaTecnica', on_delete=models.SET_NULL, null=True, blank=True,
    related_name='mantenimientos_generados',
)
```

No son dos formas de decir lo mismo: **son dos niveles**. La visita es "el técnico fue a
la farmacia"; los mantenimientos son "lo que hizo estando ahí". Una visita puede generar
varios mantenimientos, y el modelo ya lo expresa desde la migración `0016`.

Fusionarlos no simplificaría nada: obligaría a inventar una forma de representar "estas
cinco órdenes son del mismo viaje", que es exactamente lo que el FK ya hace.

### 2. La app móvil usa las visitas como entidad propia

`VisitaTecnica` tiene su ViewSet REST (`api-visita`), y la app Flutter tiene pantalla
propia y **cola offline específica**:

```dart
// movil-campo/lib/nucleo/almacen/cola_offline.dart
static const tipoIniciarVisita = 'iniciar_visita';
static const tipoCerrarVisita  = 'cerrar_visita';
```

Absorberla en `Mantenimiento` rompe la app móvil: dos endpoints, una pantalla y dos tipos
de operación encolada offline. En farmacias con enlace intermitente esa cola es lo que
evita que el técnico pierda el trabajo del día.

---

## Comparación campo por campo

| Concepto | `Mantenimiento` | `ActividadPlanificada` | `VisitaTecnica` |
|---|---|---|---|
| **A qué cuelga** | Equipo(s) vía `MantenimientoEquipo` | `equipo` **o** `ubicacion` (los dos opcionales) | `farmacia` (**requerido**) |
| Técnico | opcional | **requerido** | **requerido** |
| Quién lo creó | *(no hay campo)* | `creado_por` | `creado_por` |
| Estados | pendiente/en_proceso/cerrado/cancelado | pendiente/en_progreso/completada/cancelada | planificada/en_curso/realizada/cancelada |
| Fechas | `fecha_programada` (datetime) | `fecha_inicio`+`fecha_fin` (**rango de días**) | `fecha_planificada` (date) |
| Tiempo estimado | — | `tiempo_estimado_minutos` | — |
| Tiempo real | ✅ | ✅ | — |
| Verificación GPS | ✅ `distancia_verificacion_metros` | — | ✅ |
| Multi-tenant propio | vía `cliente` | `unidad_negocio` (**nullable = actividad interna**) | vía `farmacia` |
| Baja lógica | — | `activo` (bool) | — |

### Lo que solo tiene `Mantenimiento`

Confirmado en el código, no asumido:

- **Historial**: `EventoMantenimiento`, con nueve tipos de evento y `PROTECT` para que no
  se borre.
- **SLA**: `AcuerdoNivelServicio` por tipo y unidad de negocio.
- **Evidencia**: `FirmaMantenimiento`, `ImagenMantenimiento`, `RepuestoUtilizado`.
- **Checklist**: `ActividadChecklist` + `ActividadRealizada`.
- **Informe PDF**: `informe_pdf`, generado por tarea Celery.
- **Snapshot del equipo** al momento de abrirlo (`snapshot_equipo`).
- **Notificaciones**: al asignar, al vencer, al atrasarse y —desde el 26-sep— al cerrar.

Los otros dos **no tienen ninguna de esas siete cosas**.

### Lo que `Mantenimiento` NO puede hacer hoy

```python
# apps/mantenimiento/services.py
if not equipos:
    raise ValueError('Un mantenimiento debe tener al menos un equipo.')
```

**Exige equipo.** Absorber una visita —que cuelga de farmacia y puede no tocar ningún
equipo— no es migrar datos: es cambiar esa regla, y con ella el sentido de
`MantenimientoEquipo`, el snapshot y el conflicto "ya hay uno abierto para este equipo".

---

## Quién depende de cada uno

| | Le apuntan | Consumidores |
|---|---|---|
| `Mantenimiento` | **9 modelos**, incluido `monitoreo.Alerta.mantenimiento` | panel (18 vistas), API móvil, 3 tareas Celery |
| `VisitaTecnica` | 1 (`Mantenimiento.visita`) | **API móvil con pantalla y cola offline propias**, panel (`personas.py`) |
| `ActividadPlanificada` | 1 (`Notificacion`) | panel (3 vistas). **Sin API móvil** |

---

## Recomendación

### `VisitaTecnica`: **no fusionar**

No es un duplicado sino el nivel de arriba, y ya está enlazada. Fusionarla costaría
romper la app móvil y perder la agrupación por viaje, a cambio de nada.

**Lo que sí conviene**: la pantalla del panel no muestra los mantenimientos generados por
cada visita, aunque el FK existe. Aprovechar la relación es más valioso que borrarla.

### `ActividadPlanificada`: **el candidato real, pero no por duplicación**

Es la única de las tres que no tiene API móvil ni historial, y la única cuyo aporte
propio es discutible. Pero **tiene tres cosas que `Mantenimiento` no**:

1. **Rango de fechas** (`fecha_inicio`/`fecha_fin`): un mantenimiento tiene un instante
   programado. "Inventario de la bodega, martes a jueves" no se expresa igual.
2. **`unidad_negocio` nullable = actividad interna**, visible para todos. Un
   mantenimiento cuelga de un cliente.
3. **Trabajo sin equipo ni farmacia**: puede colgar solo de `ubicacion`, o de nada.

Si esos tres casos no se usan en la práctica, se absorbe. **Y ahí está el problema:** no
puedo medirlo.

---

## El dato que falta

**No pude contar filas en producción**: el servidor (`10.111.6.20`) no responde desde el
25-sep. La base local es de desarrollo y tiene 1 mantenimiento, 0 actividades y 0 visitas
— no dice nada.

**Sin esos tres números, la decisión no se puede tomar**:

```sql
SELECT count(*) FROM mantenimiento;
SELECT count(*), count(*) FILTER (WHERE unidad_negocio_id IS NULL) FROM actividad_planificada;
SELECT count(*) FROM visita_tecnica;
```

Y sobre `actividad_planificada`, específicamente: cuántas usan rango real
(`fecha_fin > fecha_inicio`) y cuántas no tienen ni equipo ni ubicación. Si son cero, sus
tres diferencias son teóricas.

---

## Plan de migración, si se decide absorber `ActividadPlanificada`

**Solo para ese caso.** `VisitaTecnica` no se migra.

| Campo origen | Destino | Nota |
|---|---|---|
| `titulo` + `descripcion` | `descripcion` | Se concatenan: no hay título en `Mantenimiento` |
| `tecnico` | `tecnico` | directo |
| `fecha_inicio` | `fecha_programada` | **se pierde `fecha_fin`** |
| `estado` | `estado_interno` | `completada`→`cerrado`, `en_progreso`→`en_proceso` |
| `prioridad` | `prioridad` | verificar que los valores coincidan |
| `tiempo_real_minutos` | `tiempo_real_minutos` | directo |
| `equipo` | `MantenimientoEquipo` | **las que no tienen equipo no se pueden migrar** |
| `ubicacion` | *(sin destino)* | se perdería, o va a `descripcion` como texto |
| `unidad_negocio` | vía `cliente` | las internas (null) **no tienen equivalente** |
| `tiempo_estimado_minutos` | *(sin destino)* | se perdería |
| `observaciones` | `descripcion` | se concatena |

**Tres campos sin destino y dos casos sin migrar** — actividades sin equipo, y las
internas sin unidad de negocio. Eso no es un detalle de mapeo: es la prueba de que los
modelos no son equivalentes.

---

## Riesgo y reversibilidad

**Alto, y peor de lo que parece.** La migración de datos es reversible con un backup, pero
el problema no es ese:

1. **Se pierde información que no estaba en ningún otro lado** — rango de fechas, tiempo
   estimado, ubicación. Volver atrás recupera las filas, no lo que se descartó.
2. **`Mantenimiento` tendría que aceptar trabajo sin equipo** para absorber los casos que
   hoy no lo tienen. Ese cambio toca el conflicto de "ya hay uno abierto", el snapshot y
   nueve modelos que le apuntan — y **no se revierte con `git revert`** una vez que hay
   filas nuevas apoyadas en la regla relajada.
3. **Ahorra poco**: `ActividadPlanificada` son ~100 líneas de modelo y 3 vistas.

### Lo que haría en cambio

**Nada, por ahora.** Y en concreto:

1. **Medir primero** los tres números de arriba, cuando el servidor vuelva.
2. Si `actividad_planificada` está en cero: **no migrar, desactivar** — sacar la pantalla
   del menú y dejar el modelo. Cuesta nada y es totalmente reversible.
3. Si tiene filas: dejarla. Las tres diferencias serían reales.
4. **`VisitaTecnica` se queda** en cualquier escenario. Lo que falta ahí no es fusionarla
   sino mostrar sus mantenimientos generados en el panel.
