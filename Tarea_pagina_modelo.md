# Tarea 5: actualizar la página /modelo/ con la evaluación vigente

## Por qué

La página se presenta como "documentación técnica de la investigación", así que es la
vista que se mostraría en una sustentación. Hoy muestra métricas desactualizadas y
mezcla resultados medidos sobre datasets distintos en la misma columna.

## Problema 1: métricas de datasets distintos en la misma tabla

En el historial, la fila del 10/09/2026 (Base preentrenado, nivel Producto) muestra
R² 0,9034 y MAE 5,42. Verificar primero sobre qué datos se midió: casi con seguridad
sobre el test del dataset externo (Kaggle), no sobre los datos de la microempresa.

Si se confirma, el problema es que esa fila aparece junto a las del modelo ajustado,
cuyas métricas sí vienen de los datos de la empresa. Comparar 0,9034 con 0,3407 no
tiene sentido: miden dos poblaciones distintas.

Corrección:
- Agregar a `ModeloEntrenado` un campo que registre **sobre qué conjunto se midió**
  cada métrica (externo / interno), con migración. Rellenarlo para los registros
  existentes según la fase.
- Mostrarlo como columna en el historial, o separar el historial en dos bloques.
- Si una métrica no es comparable con las demás, que la tabla lo diga en la propia
  fila, no en una nota al pie.

No borrar ni editar registros históricos. Solo etiquetarlos correctamente.

## Problema 2: dos modelos marcados "Activo"

El base preentrenado y el ajustado aparecen los dos como "Activo". Es correcto —cumplen
funciones distintas— pero la etiqueta no lo comunica.

Cambiar a etiquetas que digan la función: por ejemplo "Base vigente" para el
preentrenado y "En producción" para el que genera los pronósticos. Que de un vistazo se
entienda que no son dos modelos compitiendo por el mismo puesto.

## Problema 3: la página no refleja la evaluación actual

Reemplazar el panel "Comparación de los tres modelos" —que usa la división única y R²
como métrica principal— por la evaluación vigente. La página debe mostrar:

**(a) Métricas del modelo en producción**, en el horizonte de ventana de lead time
(el principal) y en el diario (secundario):
- Exactitud (1 − WAPE) por categoría y total
- WAPE, MAE, RMSE
- Acierto dentro de la tolerancia del 20 %
- R² dentro de cada categoría, y el R² global marcado como no interpretable por sí solo
- Dispersión del WAPE entre orígenes

**(b) Comparación contra las tres líneas base**, con el conteo por origen y la prueba de
signos.

**(c) El estudio de ablación**: las cuatro configuraciones con su exactitud y el sesgo
de accesorio.

Todo esto ya está calculado y guardado en `artefactos/evaluacion/` y
`artefactos/ablacion/`. La página debe leer el reporte más reciente, no recalcular al
cargar.

## Textos que deben aparecer en la página

Tres advertencias, redactadas en la vista y no solo en el README, porque la página se
muestra sola:

1. Junto al R² global: que mezcla categorías de volúmenes muy distintos y que por sí
   solo sobreestima el desempeño; la lectura correcta es el R² dentro de cada categoría.
2. Junto a la prueba de signos: que los orígenes se solapan —comparten seis de sus siete
   días— y que los valores p indican consistencia, no significancia estadística.
3. Junto a las filas de nivel Producto: que corresponden al enfoque por producto que se
   descartó por densidad insuficiente de demanda, y que no son comparables con el
   enfoque por categoría vigente.

## Diseño

Mantener el estilo actual de la página. Las tablas nuevas son más anchas que las
actuales: verificar que se lean bien y que no desborden horizontalmente en pantallas
de portátil. Si una tabla no cabe, partirla por horizonte en lugar de encogerla.

## Lo que no hay que hacer

- No recalcular métricas al cargar la página. Leer los reportes guardados.
- No borrar ni modificar registros históricos de `ModeloEntrenado`.
- No reentrenar ni activar modelos.
- No tocar los datos.

## Tests

- El campo de conjunto de medición se rellena correctamente en la migración para los
  registros existentes.
- La vista muestra el reporte más reciente y no falla si falta alguno de los dos
  (evaluación o ablación): debe degradar mostrando lo que sí existe.
- Las tres advertencias están presentes en el HTML renderizado.
- Las filas de nivel Producto quedan marcadas como enfoque descartado.

## Entregable

Una captura o descripción de cómo quedó la página, y confirmación de que los números
mostrados coinciden con los reportes JSON. Actualizar README y CLAUDE.md.
