# Tarea: métricas de exactitud del pronóstico

## Objetivo

Agregar al sistema el cálculo de **exactitud del pronóstico** (métrica estándar de
gestión de inventario) junto a las métricas actuales, y la línea base contra la cual
comparar. Hoy `evaluar_predicciones` reporta MAE, RMSE, SMAPE y R², que no permiten
afirmar nada sobre exactitud en el sentido que se usa en logística.

## Regla que no se negocia

El margen de tolerancia y la definición de cada métrica se fijan **antes** de correr
la evaluación y no se ajustan después de ver el resultado. Si una categoría queda por
debajo del umbral, se reporta así. No se eligen umbrales por el valor que producen.

## 1. Métricas nuevas en `inventario/ml/evaluacion.py`

### 1.1 WAPE y exactitud

```
WAPE = Σ |real - pronóstico| / Σ real
exactitud = 1 - WAPE
```

Calcular por categoría y en total (el total es un WAPE agregado sobre todas las
filas, no el promedio de los WAPE por categoría — son cosas distintas).

Casos borde:
- Si `Σ real == 0` para una categoría en el periodo, WAPE no está definido: devolver
  `None` y no incluir esa categoría en el total.
- La exactitud puede salir negativa cuando el error supera la demanda real
  (pasa en categorías de baja rotación). No truncar a 0: reportar el valor real.

### 1.2 Acierto dentro de tolerancia

Dos variantes, ambas por categoría:
- **Relativa:** porcentaje de días donde `|real - pronóstico| / max(real, 1) <= t`
- **Absoluta:** porcentaje de días donde `|real - pronóstico| <= u` unidades

Los valores de `t` y `u` vienen de configuración, no hardcodeados:
`PRONOSTICO_TOLERANCIA_RELATIVA` y `PRONOSTICO_TOLERANCIA_UNIDADES` en `.env`,
leídos con `decouple`. Documentar en `.env.example` con el criterio de negocio que
justifica el valor elegido (ej. tamaño del lote de compra del proveedor).

### 1.3 R² dentro de cada categoría

Hoy el R² se calcula sobre todas las filas juntas. Como las tres categorías tienen
niveles muy distintos (esmalte ~65 u/día, látex ~10, accesorios ~7), la varianza
entre categorías domina y el R² global queda inflado: mide la capacidad de distinguir
categorías, no de predecir demanda.

Agregar `r2_por_categoria`: el R² calculado dentro de cada categoría por separado.
Mantener el R² global para no romper lo ya reportado, pero en la salida del comando
dejar claro cuál es cuál.

## 2. Líneas base en el mismo módulo

Tres, todas evaluadas sobre el mismo test que el modelo:

1. **Media por categoría** del periodo de entrenamiento, repetida para todos los días
   del test. Esta es la línea base relevante para un R² agrupado por categoría y hoy
   no existe en el código.
2. **Media móvil de 7 días** por categoría, encadenada día a día.
3. **Último valor** (naive / persistencia).

Reportar para cada una las mismas métricas que para el modelo. El modelo solo puede
presentarse como aportando valor si supera a las tres.

## 3. Comando `evaluar_predicciones`

Extender la salida con una tabla por categoría: demanda real, pronosticada, MAE,
WAPE, exactitud, acierto dentro de tolerancia, R² intra-categoría. Y una segunda
tabla con las tres líneas base y el modelo, para comparación directa.

Mantener la salida actual; solo agregar. Respetar `--origen` (real por defecto) y la
división temporal existente: nunca `train_test_split` con shuffle.

Guardar el resultado en JSON en `artefactos/evaluacion/` con la fecha, igual que hace
`optimizar_modelo`, para poder citar la corrida exacta en la tesis.

## 4. Persistencia

Agregar a `ModeloEntrenado` (migración nueva) los campos de la corrida de evaluación:
`wape`, `exactitud`, `r2_intra_categoria` (JSON por categoría), `acierto_tolerancia`
(JSON por categoría), y las métricas de la mejor línea base para comparación.
Read-only en el admin.

## 5. Tests

En `inventario/tests/test_evaluacion.py`:

- WAPE con valores conocidos a mano (ej. real [10,10], pronóstico [8,14] → WAPE 0.30,
  exactitud 0.70).
- WAPE con `Σ real == 0` devuelve `None` y la categoría queda fuera del total.
- Exactitud negativa cuando el error supera la demanda, sin truncar.
- El total es WAPE agregado y **no** el promedio de los WAPE por categoría: construir
  un caso donde los dos valores difieran y verificar que se devuelve el agregado.
- R² intra-categoría distinto del global en un caso con niveles separados.
- Las tres líneas base se calculan sobre el mismo test que el modelo.
- La media por categoría usa solo días de entrenamiento (ningún día del test entra en
  su cálculo).
- Las tolerancias se leen de configuración y cambiarlas cambia el resultado.
- El comando no activa ni registra ningún modelo.

## 6. Documentación

En el README, una sección "Exactitud del pronóstico" con:
- la definición de cada métrica y su fórmula;
- el criterio de negocio que fija la tolerancia;
- la aclaración de que R² y exactitud miden cosas distintas: R² mide cuánto del
  movimiento día a día se explica, WAPE mide el tamaño del error relativo al volumen.
  Para dimensionar pedidos la segunda es la más relevante; ninguna reemplaza a la otra.
- la tabla de resultados de la corrida, con las líneas base al lado.

## Lo que NO hay que hacer

- No elegir tolerancias por el resultado que producen.
- No excluir categorías de baja rotación del reporte para que el total suba.
- No reemplazar el R² por la exactitud: van las dos, con la explicación de qué mide
  cada una.
- No tocar los datos. El origen sigue siendo `real` y el periodo sigue siendo agosto.

## Entregable

Correr el comando con el modelo vigente y pegar la tabla de resultados, incluyendo
las líneas base. Si el modelo no supera a la media móvil, decirlo: ese también es un
resultado reportable y justifica reentrenar cuando haya más histórico.
