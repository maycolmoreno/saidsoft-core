# Diagrama de Clases — SAIDSOFT

Dado el tamaño del sistema (más de 30 modelos en ~10 apps Django), el diagrama se separa
por dominio funcional en vez de un único diagrama monolítico ilegible. Cada bloque usa
sintaxis Mermaid `classDiagram` y puede pegarse tal cual en cualquier visor compatible
(GitHub, GitLab, VS Code con extensión Mermaid, mermaid.live).

---

## Dominio: Scripts, Despliegues y Software

```mermaid
classDiagram
    class Script {
        +nombre: CharField
        +descripcion: TextField
        +tipo: TipoScript = powershell
        +contenido: TextField
        +categoria: CharField
        +es_adhoc: bool
        +activo: bool
        +fecha_creacion: DateTimeField
        +fecha_modificacion: DateTimeField
    }
    class EjecucionScript {
        +contenido_snapshot: TextField
        +sha256: CharField
        +destino_tipo: DestinoTipo
        +timeout_segundos: int
        +parametros: JSONField
        +estado: Estado
        +fecha_creacion: DateTimeField
    }
    class Estado_EjecucionScript {
        <<enumeration>>
        PENDIENTE_APROBACION
        PENDIENTE
        EN_PROGRESO
        COMPLETADO
        CON_ERRORES
    }
    class ResultadoEjecucionScript {
        +estado: Estado
        +exit_code: int
        +stdout: TextField
        +stderr: TextField
        +fecha_envio: DateTimeField
        +fecha_inicio: DateTimeField
        +fecha_fin: DateTimeField
    }
    class ScriptProgramado {
        +destino_tipo: DestinoTipo
        +frecuencia_dias: int
        +timeout_segundos: int
        +fecha_ultima_ejecucion: DateField
        +fecha_proxima_ejecucion: DateField
        +activo: bool
    }
    class Despliegue {
        +version: CharField
        +archivo: FileField
        +sha256: CharField
        +descripcion: TextField
        +modo_aplicacion: ModoAplicacion
        +ventana_fecha_hora: DateTimeField
        +destino_tipo: DestinoTipo
        +estado: Estado
        +umbral_error_pct: Decimal
        +freno_omitido: bool
        +fecha_creacion: DateTimeField
        +fecha_publicacion: DateTimeField
    }
    class Estado_Despliegue {
        <<enumeration>>
        BORRADOR
        PENDIENTE_APROBACION
        APROBADO
        PUBLICANDO
        PAUSADO
        COMPLETADO
        CANCELADO
    }
    class ResultadoDespliegue {
        +estado: Estado
        +version_previa: CharField
        +version_nueva: CharField
        +detalle_error: TextField
        +fecha_actualizacion: DateTimeField
    }
    class EventoDespliegue {
        +paso: Paso
        +detalle: TextField
        +timestamp: DateTimeField
    }
    class AplicacionCatalogo {
        +nombre: CharField
        +fabricante: CharField
        +categoria: CharField
        +comando_deteccion: TextField
        +version_mas_reciente_conocida: CharField
        +activo: bool
    }
    class VersionAplicacion {
        +version: CharField
        +instalador: FileField
        +sha256: CharField
        +tamanio_bytes: int
        +comando_instalacion_silenciosa: TextField
        +comando_desinstalacion: TextField
        +fecha_publicacion: DateTimeField
    }
    class SolicitudInstalacion {
        +accion: TipoAccionInstalacion
        +destino_tipo: DestinoTipo
        +estado: EstadoSolicitud
        +fecha_creacion: DateTimeField
    }
    class ResultadoInstalacion {
        +estado: Estado
        +fecha_actualizacion: DateTimeField
    }
    class SoftwareInstaladoDetectado {
        +nombre: CharField
        +version: CharField
        +fabricante: CharField
        +detectado_en: DateTimeField
    }
    class InventarioProgramado {
        +destino_tipo: DestinoTipo
        +frecuencia_dias: int
        +activo: bool
    }

    Script "1" --> "*" EjecucionScript : script
    EjecucionScript "1" --> "*" ResultadoEjecucionScript : ejecucion
    EjecucionScript --> Estado_EjecucionScript : estado
    ScriptProgramado "1" --> "*" EjecucionScript : programado (opcional)
    Script "1" --> "*" ScriptProgramado : script
    EjecucionScript --> Estacion : estaciones (M2M)
    EjecucionScript --> Farmacia : farmacias (M2M)
    EjecucionScript --> Grupo : grupos (M2M)

    Despliegue --> Estado_Despliegue : estado
    Despliegue "1" --> "*" ResultadoDespliegue : despliegue
    ResultadoDespliegue "1" --> "*" EventoDespliegue : resultado
    Despliegue "1" --> "0..1" Despliegue : despliegue_origen (promoción)
    Despliegue --> Estacion : estaciones (M2M)
    Despliegue --> Farmacia : farmacias (M2M)
    Despliegue --> Grupo : grupos (M2M)

    AplicacionCatalogo "1" --> "*" VersionAplicacion : aplicacion
    VersionAplicacion "1" --> "*" SolicitudInstalacion : version_aplicacion
    SolicitudInstalacion "1" --> "*" ResultadoInstalacion : solicitud
    ResultadoInstalacion "1" --> "*" EventoInstalacion
    Estacion "1" --> "*" SoftwareInstaladoDetectado : estacion
```

*(`Estacion`, `Farmacia`, `Grupo`, `UnidadNegocio` se detallan en el dominio "Catálogo y
Estaciones" — se repiten acá solo como referencia de relación.)*

---

## Dominio: Catálogo y Estaciones

Este es el dominio raíz del sistema — casi todos los demás dominios cuelgan de
`UnidadNegocio`, `Farmacia` o `Estacion`.

```mermaid
classDiagram
    class UnidadNegocio {
        +codigo: CharField
        +nombre: CharField
        +activo: bool
    }
    class Grupo {
        +codigo: CharField
        +nombre: CharField
        +version_objetivo: CharField
        +activo: bool
    }
    class Farmacia {
        +codigo: CharField
        +nombre: CharField
        +ciudad: CharField
        +provincia: CharField
        +direccion: CharField
        +tipo_sucursal: TipoSucursal
        +formato_farmacia: FormatoFarmacia
        +latitud: float
        +longitud: float
        +ip_router: GenericIPAddress
        +segmento_red: CharField
        +tipo_enlace: CharField
        +tiene_backup: bool
        +activa: bool
    }
    class Estacion {
        +codigo: CharField
        +hostname: CharField
        +numero_serie: CharField
        +so_nombre: CharField
        +version_agente: CharField
        +version_pos: CharField
        +bitlocker_habilitado: bool
        +estado_conexion: EstadoConexion
        +estado_aprobacion: EstadoAprobacion
        +ultimo_heartbeat: DateTimeField
        +token_enrolamiento: CharField
        +hardware_id: CharField
        +meshcentral_node_id: CharField
        +windows_update_pendientes: int
    }
    class EstadoConexion {
        <<enumeration>>
        NUNCA_CONECTADA
        ONLINE
        OFFLINE
    }
    class EstadoAprobacion {
        <<enumeration>>
        PENDIENTE
        APROBADA
        RECHAZADA
    }
    class VersionAgente {
        +version: CharField
        +sha256: CharField
        +tamanio_bytes: int
        +notas: TextField
    }
    class ClaveRecuperacionBitLocker {
        +clave_cifrada: TextField
        +id_protector: CharField
        +actualizada_en: DateTimeField
    }

    UnidadNegocio "1" --> "*" Farmacia : unidad_negocio
    Grupo "1" --> "*" Farmacia : grupo
    Farmacia "1" --> "*" Estacion : farmacia
    Estacion --> EstadoConexion : estado_conexion
    Estacion --> EstadoAprobacion : estado_aprobacion
    Estacion "1" --> "0..1" ClaveRecuperacionBitLocker : clave_bitlocker (OneToOne)
    Farmacia "0..1" --> "1" Colaborador : tecnico_asignado
```

**Permisos custom sobre `Estacion`** (no son CRUD estándar, se otorgan aparte):
`acceso_remoto_estacion`, `supervision_auditoria_estacion`, `ver_clave_bitlocker`,
`consultar_info_estacion`, `aprobar_estacion`, `reiniciar_estacion`,
`escanear_actualizaciones_estacion`, `actualizar_agente_estacion`.

## Dominio: Monitoreo, Cumplimiento y Mantenimiento

```mermaid
classDiagram
    class MuestraMetrica {
        +cpu_carga_pct: float
        +ram_usada: int
        +disco_libre_gb: float
        +temperatura_c: float
        +latencia_ms: float
        +red_recibido_kbps: float
        +red_enviado_kbps: float
        +timestamp: DateTimeField
    }
    class MuestraRedFarmacia {
        +bytes_recibidos: BigInt
        +bytes_enviados: BigInt
        +red_recibido_kbps: float
        +red_enviado_kbps: float
        +timestamp: DateTimeField
    }
    class ReglaAlerta {
        +nombre: CharField
        +metrica: Metrica
        +operador: Operador
        +umbral: float
        +duracion_minutos: int
        +severidad: Severidad
        +activo: bool
    }
    class Alerta {
        +estado: Estado
        +valor_disparador: float
        +abierta_en: DateTimeField
        +reconocida_en: DateTimeField
        +resuelta_en: DateTimeField
        +escalada_en: DateTimeField
    }
    class Estado_Alerta {
        <<enumeration>>
        ABIERTA
        RECONOCIDA
        RESUELTA
    }
    class VentanaMantenimiento {
        +destino_tipo: DestinoTipo
        +desde: DateTimeField
        +hasta: DateTimeField
        +motivo: CharField
        +activo: bool
    }
    class CanalNotificacion {
        +tipo: Tipo
        +destino: URLField
        +activo: bool
    }
    class ActividadCumplimiento {
        +nombre: CharField
        +tipo_objetivo: TipoObjetivo
        +fecha_limite: DateField
    }
    class ResultadoCumplimientoEstacion {
        +estado: EstadoCumplimiento
        +fecha_completado: DateTimeField
        +observacion: CharField
    }
    class Mantenimiento {
        +descripcion: TextField
        +tipo_origen: TipoOrigen
        +estado_interno: EstadoInterno
        +resultado_tecnico: ResultadoTecnico
        +estado_general: EstadoGeneralEquipo
        +fecha_programada: DateTimeField
        +fecha_cierre: DateTimeField
        +informe_pdf: FileField
    }
    class MantenimientoProgramado {
        +frecuencia_dias: int
        +fecha_ultimo: DateField
        +fecha_proximo: DateField
        +activo: bool
    }
    class RepuestoUtilizado {
        +cantidad: int
        +costo_unitario: Decimal
    }
    class FirmaMantenimiento {
        +tipo_firma: TipoFirma
        +firma_base64: TextField
        +firmado_en: DateTimeField
    }
    class ActividadPlanificada {
        +titulo: CharField
        +prioridad: Prioridad
        +estado: Estado
        +fecha_inicio: DateTimeField
        +fecha_fin: DateTimeField
    }
    class SoftwareInstaladoDetectado {
        +nombre: CharField
        +version: CharField
        +detectado_en: DateTimeField
    }
    class AplicacionCatalogo {
        +nombre: CharField
        +version_mas_reciente_conocida: CharField
    }

    Estacion "1" --> "*" MuestraMetrica : estacion
    Farmacia "1" --> "*" MuestraRedFarmacia : farmacia
    UnidadNegocio "1" --> "*" ReglaAlerta : unidad_negocio (opcional)
    ReglaAlerta "1" --> "*" Alerta : regla
    Estacion "1" --> "*" Alerta : estacion
    Alerta --> Estado_Alerta : estado
    UnidadNegocio "1" --> "*" VentanaMantenimiento : unidad_negocio
    UnidadNegocio "1" --> "*" CanalNotificacion : unidad_negocio (opcional)

    ActividadCumplimiento "1" --> "*" ResultadoCumplimientoEstacion : actividad
    Estacion "1" --> "*" ResultadoCumplimientoEstacion : estacion

    Activo "*" --> "*" Mantenimiento : equipo (vía MantenimientoEquipo)
    Colaborador "1" --> "*" Mantenimiento : cliente
    Mantenimiento "1" --> "*" RepuestoUtilizado : mantenimiento
    Mantenimiento "1" --> "*" FirmaMantenimiento : mantenimiento
    MantenimientoProgramado "1" --> "*" Mantenimiento : mantenimiento_programado (opcional)
    Activo "1" --> "*" MantenimientoProgramado : equipo
    ActividadPlanificada --> Mantenimiento : mantenimiento (opcional)

    Estacion "1" --> "*" SoftwareInstaladoDetectado : estacion
    AplicacionCatalogo "1" --> "*" VersionAplicacion : aplicacion
```

*(`Activo`/`Colaborador` se detallan en el dominio "Activos e Inventario";
`VersionAplicacion` en el dominio "Scripts, Despliegues y Software".)*

## Dominio: Activos e Inventario

```mermaid
classDiagram
    class Departamento {
        +nombre: CharField
        +tipo: Tipo
        +activo: bool
    }
    class Cargo {
        +nombre: CharField
        +activo: bool
    }
    class Ubicacion {
        +nombre: CharField
        +agencia: CharField
        +latitud: Decimal
        +longitud: Decimal
        +direccion: CharField
        +ciudad: CharField
        +provincia: CharField
        +activo: bool
    }
    class Colaborador {
        +nombre: CharField
        +cedula: CharField
        +correo: EmailField
        +telefono: CharField
        +sucursal: CharField
        +zona: CharField
        +fecha_ingreso: DateField
        +activo: bool
    }
    class CategoriaEquipo {
        +codigo: CharField
        +nombre: CharField
    }
    class Marca {
        +nombre: CharField
    }
    class Bodega {
        +codigo: CharField
        +nombre: CharField
        +ubicacion: CharField
        +activa: bool
    }
    class TipoConsumible {
        +codigo: CharField
        +nombre: CharField
        +stock_minimo: int
    }
    class StockBodega {
        +cantidad: int
    }
    class OrdenCompra {
        +numero_oc: CharField
        +proveedor: CharField
        +fecha_emision: DateField
        +estado: Estado
        +version: int
    }
    class Estado_OrdenCompra {
        <<enumeration>>
        BORRADOR
        EMITIDA
        RECEPCION_PARCIAL
        RECIBIDA
        CANCELADA
    }
    class OrdenCompraDetalle {
        +tipo_item: TipoItem
        +descripcion: CharField
        +cantidad_solicitada: int
        +cantidad_recibida: int
        +precio_unitario: Decimal
        +estado: Estado
        +version: int
    }
    class RecepcionLote {
        +uuid: UUIDField
        +numero_lote: CharField
        +cantidad_recibida: int
        +estado: Estado
        +fecha_recepcion: DateTimeField
    }
    class MovimientoInventario {
        +tipo_movimiento: TipoMovimiento
        +cantidad: int
        +motivo: CharField
        +fecha_efectiva: DateTimeField
    }
    class Activo {
        +codigo: CharField
        +tipo: Tipo
        +modelo: CharField
        +numero_serie: CharField
        +procesador: CharField
        +ram_gb: int
        +almacenamiento_gb: int
        +vencimiento_garantia: DateField
        +baja_recomendada: bool
        +estado: Estado
        +estado_fisico_actual: EstadoFisico
    }
    class Estado_Activo {
        <<enumeration>>
        EN_BODEGA
        ASIGNADO
        EN_REPARACION
        DADO_DE_BAJA
    }
    class EventoActivo {
        +tipo_evento: TipoEvento
        +detalle: JSONField
        +timestamp: DateTimeField
    }

    Departamento "1" --> "*" Cargo : departamento
    Departamento "1" --> "*" Ubicacion : departamento
    Cargo "1" --> "*" Colaborador : cargo
    Ubicacion "1" --> "*" Colaborador : ubicacion
    Colaborador "0..1" --> "1" AuthUser : usuario (OneToOne)
    Colaborador "1" --> "*" Ubicacion : encargado

    Bodega "1" --> "*" StockBodega : bodega
    TipoConsumible "1" --> "*" StockBodega : tipo_consumible
    Bodega "*" --> "*" OrdenCompra : bodegas_destino
    OrdenCompra --> Estado_OrdenCompra : estado
    OrdenCompra "1" --> "*" OrdenCompraDetalle : orden_compra
    CategoriaEquipo "1" --> "*" OrdenCompraDetalle
    Marca "1" --> "*" OrdenCompraDetalle
    OrdenCompraDetalle "1" --> "*" RecepcionLote : orden_compra_detalle
    RecepcionLote "1" --> "*" MovimientoInventario : recepcion_lote (opcional)
    Bodega "1" --> "*" MovimientoInventario : bodega_origen/bodega_destino
    RecepcionLote --> Bodega : bodega_destino
    RecepcionLote --> Colaborador : custodio_receptor

    Activo --> Estado_Activo : estado
    Activo --> Marca : marca
    Activo --> CategoriaEquipo : categoria
    Activo "0..1" --> "1" Estacion : estacion (OneToOne, vínculo RMM)
    Activo --> Bodega : bodega_actual
    Activo --> Colaborador : colaborador_actual
    Activo --> OrdenCompra : orden_compra
    Activo "1" --> "*" EventoActivo : activo
```

*(`AuthUser` = `django.contrib.auth.User`; `Estacion` se detalla en el dominio
"Catálogo y Estaciones".)*

## Dominio: Cuentas, Auditoría y Facturación

```mermaid
classDiagram
    class PerfilUsuario {
        +fcm_token: CharField
        +acceso_todas_unidades: bool
    }
    class EventoAuditoria {
        +accion: CharField
        +modelo: CharField
        +objeto_id: CharField
        +objeto_repr: CharField
        +detalle: JSONField
        +ip_address: GenericIPAddressField
        +timestamp: DateTimeField
    }
    class ActividadMensualEstacion {
        +anio: int
        +mes: int
        +primer_heartbeat_en: DateTimeField
    }
    class AuthUser {
        <<django.contrib.auth>>
        +username: CharField
        +email: EmailField
        +is_staff: bool
        +is_superuser: bool
    }
    class AuthGroup {
        <<django.contrib.auth>>
        +name: CharField
    }

    AuthUser "1" --> "0..1" PerfilUsuario : usuario (OneToOne)
    PerfilUsuario "*" --> "*" UnidadNegocio : unidades_negocio
    PerfilUsuario "0..1" --> "1" Colaborador : colaborador
    AuthUser "*" --> "*" AuthGroup : groups (roles)
    AuthUser "1" --> "*" EventoAuditoria : usuario
    EventoAuditoria --> UnidadNegocio : unidad_negocio (derivado, opcional)
    Estacion "1" --> "*" ActividadMensualEstacion : estacion
```

*(`UnidadNegocio`/`Estacion` se detallan en el dominio "Catálogo y Estaciones";
`Colaborador` en el dominio "Activos e Inventario". `AuthGroup` es el mecanismo real
de "rol" del sistema — no existe un modelo `Rol` propio.)*
