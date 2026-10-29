# Tarea 9: cuadrar el COI del pretest y dejar el sistema listo para el postest

## Parte A — Diferencia del COI entre la ficha y el sistema

### Hecho observado

| | Ficha Excel | Sistema |
|---|---|---|
| Líneas con atención incompleta | 91 | 91 |
| Unidades no atendidas | 602 | 602 |
| Productos afectados | 79 | 79 |
| Costos de almacenamiento (CA) | 1 050,00 | 1 050,00 |
| **COI total** | **9 792,80** | **5 421,40** |

Los datos coinciden; solo difiere el bloque de pérdidas por desabastecimiento (PD).
PD del sistema = 4 371,40. PD implícito en la ficha = 9 792,80 − 1 050,00 = 8 742,80.
El cociente es exactamente 2,0000, así que es una diferencia de fórmula, no de datos.

### A.1 Verificar la fórmula que usa el sistema

Mostrar el código exacto de `calcular_coi()` en `inventario/servicios/indicadores.py` y
confirmar, sin cambiarlo todavía, si la pérdida por línea es:

- `(solicitada − atendida) × (precio_venta − costo_compra)`  → margen, o
- `(solicitada − atendida) × precio_venta`                   → precio completo

Confirmar además si usa el precio congelado en `PedidoDetalle` o el precio actual del
producto. Esto es crítico: si lee el precio actual, el COI del pretest cambiará cuando
se actualicen precios para el postest.

### A.2 Comprobar el margen del catálogo

Ejecutar y reportar:

```sql
SELECT COUNT(*) AS total,
       COUNT(*) FILTER (WHERE precio_venta = 2 * costo_compra) AS margen_100,
       ROUND(AVG(precio_venta / NULLIF(costo_compra,0))::numeric, 4) AS ratio_medio
FROM inventario_producto WHERE costo_compra > 0;
```

Si casi todos cumplen `precio = 2 × costo`, queda explicado que una fórmula con precio
completo dé exactamente el doble que una con margen.

### A.3 Criterio que debe quedar documentado

La pérdida por desabastecimiento es la **utilidad no percibida**, no el precio de venta:
si el producto se hubiera vendido, también se habría incurrido en su costo de compra.
Es la definición de Silver, Pyke & Peterson y de Chopra & Meindl, ya citada en el
proyecto.

Conclusión esperada: **el valor correcto es el del sistema (5 421,40) y la ficha Excel es
la que debe corregirse.** Si al revisar el código resulta que el sistema es el que usa
precio completo, entonces hay que corregir el sistema y decirlo. No ajustar nada para que
coincida con la ficha.

### A.4 Inconsistencia de periodo dentro de la ficha

El bloque CA de la ficha declara el periodo **25/07/2026 – 24/08/2026**, mientras que el
bloque PD y la Ficha 2 cubren **01/08/2026 – 31/08/2026**. Los dos componentes del COI
están midiendo ventanas distintas.

Revisar cómo guarda el sistema los costos de almacenamiento (modelo, campos de periodo)
y verificar que la ventana del CA coincida con la ventana sobre la que se calculan EI,
NS y PD. Si no coincide, reportarlo con el impacto en soles; no cambiar los montos sin
decirlo.

### A.5 Desglose visible

Hacer que el reporte y la vista del indicador muestren el COI desglosado:

```
COI = CA + PD
CA  = suma de costos de almacenamiento del periodo        → S/ ____
PD  = Σ (solicitada − atendida) × margen unitario         → S/ ____
```

Con el periodo exacto y el número de líneas que entran en PD, para poder cuadrarlo contra
la ficha línea por línea.

## Parte B — Dejar el sistema listo para importar el postest

### B.1 Separación pretest / postest

Verificar y reportar cómo quedaría separado el postest del pretest:

- ¿Basta con el rango de fechas, o hace falta un valor de origen distinto?
- ¿Los comandos de carga permiten indicar el periodo al que pertenece cada registro?
- ¿Las vistas e indicadores permiten calcular un periodo arbitrario sin tocar el otro?

Proponer la opción más simple que garantice que **el pretest queda congelado**.

### B.2 Prueba de congelamiento del pretest (la más importante)

Escribir un test que demuestre que, al importar datos del postest y actualizar precios,
los tres indicadores del pretest **no cambian ni un decimal**:

1. Calcular EI, NS y COI de agosto y guardar los valores.
2. Cargar pedidos, movimientos y conteos de un periodo posterior.
3. Actualizar el precio y el costo de varios productos que aparecen en el bloque PD de agosto.
4. Volver a calcular EI, NS y COI de agosto.
5. Afirmar que los tres valores son idénticos a los del paso 1.

Si este test falla, hay que arreglarlo antes de importar nada real. Es el riesgo más
grande de toda la comparación pretest/postest.

### B.3 Qué hace falta recolectar para el postest

Listar explícitamente, a partir de lo que exige cada indicador, qué datos hay que pedirle
a la empresa para el postest, con su formato:

- Para EI: conteo físico del periodo, con responsable y fecha.
- Para NS: pedidos con cantidad solicitada, atendida, fecha requerida y fecha de atención.
- Para COI: los costos de almacenamiento del periodo (alquiler, energía, personal, mermas)
  y los precios y costos vigentes en ese periodo.

Indicar qué comandos cargan cada cosa y en qué orden.

### B.4 Checklist de importación

Dejar en el README una sección "Importar el postest" con los pasos en orden, los comandos
exactos, las validaciones que hay que correr antes y después, y cómo revertir si la carga
sale mal.

## Reglas

- No cambiar ningún número para que cuadre con la ficha. Si el sistema está mal, se
  corrige el sistema y se reporta; si la ficha está mal, se reporta la corrección que hay
  que hacerle a la ficha.
- No tocar los datos cargados del pretest.
- No activar ni registrar modelos.
- Origen real y periodo agosto siguen siendo los del pretest.

## Entregable

1. La fórmula exacta que usa el sistema hoy, citando el código.
2. El resultado de la consulta del margen.
3. El veredicto: cuál de los dos valores es correcto y por qué.
4. La corrección concreta que hay que aplicarle a la ficha Excel, en una línea.
5. El estado del periodo del CA y, si no coincide, el impacto en soles.
6. El resultado del test de congelamiento del pretest.
7. La suite completa de tests, y commit.
