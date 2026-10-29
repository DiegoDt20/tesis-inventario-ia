# Tarea 11: cargar el pretest corregido de agosto y documentar las correcciones

## Contexto

El jefe de la microempresa entregó una versión corregida de la ficha del pretest de
agosto 2026 (`PRETEST_TESIS_FINAL.xlsx`). Reemplaza a la que está cargada hoy.

Ya fue validada: mismo periodo, mismas 260 líneas, mismos clientes y mismas cantidades
solicitadas. Cambian 8 filas y dos declaraciones de periodo.

## Correcciones que trae, verificadas

| Corrección | Filas | Detalle |
|---|---|---|
| Código de producto | 5 | N.° 26 y 240: PIN-260 → PIN-219. N.° 47, 162 y 190: PIN-261 → PIN-260. El nombre del producto no cambia; es la reasignación de códigos duplicados |
| Cantidad atendida | 1 | N.° 167: de 2 a 12 unidades, con fecha de atención 10/08/2026 |
| Fecha de atención | 3 | N.° 229 (25/08), N.° 241 (19/08) y la del N.° 167 |
| Fecha de corte del conteo físico | todas | 24/08/2026 → **31/08/2026** |
| Periodo de los costos de almacenamiento | todas | 25/07–24/08 → **01/08–31/08/2026**. Los montos no cambian (S/ 1 050,00) |

Además, el bloque `3_COI_PD` ahora trae los precios y costos completos; la versión
anterior los tenía vacíos.

### Efecto esperado en los indicadores

| | Cargado hoy | Esperado tras la carga |
|---|---|---|
| EI | 56,32 % (147/261) | **56,15 %** (146/260) |
| NS | 58,08 % (151/260) | **59,23 %** (154/260) |
| COI | S/ 5 421,40 | **S/ 5 341,40** |
| Líneas en el bloque PD | 91 | 90 |

El COI baja exactamente S/ 80,00 porque la línea 167 sale del bloque de
desabastecimiento: 10 unidades que faltaban × S/ 8,00 de margen.

Si al cargar sale algo distinto de estos valores, **detener y reportar** antes de
confirmar nada.

## 1. Antes de cargar

1. Respaldar la base y decir dónde quedó el respaldo.
2. Guardar la foto de los indicadores actuales con `reporte_indicadores`, para poder
   comparar antes y después.
3. Hacer un ensayo sin escribir, dentro de una transacción que se revierta, e informar
   qué cambiaría: filas afectadas, indicadores resultantes y cualquier validación que
   rechace algo.
4. **Preguntar antes de la carga real.**

## 2. La carga

Reemplaza los datos de agosto 2026. Hay que cuidar que:

- No queden duplicados: las 260 líneas del archivo sustituyen a las 260 actuales, no se
  suman.
- Los precios y costos del bloque PD se guarden **congelados en cada línea de pedido**,
  tomados del archivo y no del catálogo actual.
- La fecha de corte del conteo físico quede en 31/08/2026.
- El periodo de los costos de almacenamiento quede en 01/08–31/08/2026.
- El responsable del conteo físico: si el archivo trae un nombre, usarlo; si sigue
  diciendo "Importación automática", reportarlo para que se corrija con el nombre real.

## 3. Después de cargar

- Recalcular stock, predicciones, recomendaciones y anomalías con los datos corregidos.
- Reportar los tres indicadores y contrastarlos contra la tabla de arriba.
- Indicar cuántas recomendaciones cambiaron de estado respecto al lote anterior.

## 4. Tabla de correcciones para el anexo de la tesis

Generar un documento con una tabla que liste **cada corrección aplicada**, con estas
columnas:

`N.° de registro · Campo corregido · Valor anterior · Valor corregido · Motivo`

Debe salir de comparar los dos archivos, no escrita a mano, para que sea verificable.
Guardarlo en `artefactos/correcciones/` con la fecha, y dejar una copia en formato
legible para pegar como anexo.

El motivo de cada grupo:

- Códigos: reasignación de códigos que identificaban dos productos distintos.
- Cantidad atendida y fechas de atención: registros incompletos al momento del primer
  levantamiento, completados por la empresa.
- Fecha de corte y periodo de costos: alineación de la ventana de medición al mes
  calendario de agosto, para que los tres indicadores midan el mismo periodo.

## 5. Tests

- Tras la carga hay exactamente 260 líneas de pedido en agosto, no 520.
- Los tres indicadores dan los valores esperados de la tabla.
- Los precios quedan congelados por línea: cambiar el precio del catálogo no mueve el COI.
- La fecha de corte del conteo es 31/08/2026 y el periodo del CA es 01/08–31/08/2026.
- La tabla de correcciones se genera comparando archivos y tiene 8 filas de registro más
  las dos de periodo.

## Reglas

- No inventar ni ajustar ningún valor para que cuadre con la tabla esperada. Si no
  cuadra, reportarlo.
- No borrar el respaldo.
- No activar ni registrar modelos nuevos sin avisar.

## Entregable

1. Resultado del ensayo sin escribir.
2. Los tres indicadores después de la carga, contrastados con los esperados.
3. La tabla de correcciones.
4. Qué cambió en las recomendaciones.
5. Suite completa de tests, y commit.
