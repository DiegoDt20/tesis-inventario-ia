# Tarea 2: evaluación sobre ventana de lead time y validación walk-forward

## Contexto

La evaluación actual usa una sola división: entrenamiento 01–24/08, prueba 25–31/08.
Son 21 filas. Con esa muestra, diferencias de 1 o 2 puntos de WAPE no son concluyentes
y el resultado depende demasiado de qué semana cayó en el test.

Además el pronóstico se evalúa día a día, pero la decisión que alimenta es semanal:
el punto de reorden usa ROP = d̄·L + SS, donde L es el lead time (7 días). Lo que el
motor necesita saber es cuánta demanda hay **durante los 7 días** que tarda en llegar
el pedido, no cuánto se vende un martes concreto.

Esta tarea corrige las dos cosas.

## Regla que no se negocia

Igual que en la tarea anterior: los umbrales, el horizonte y las ventanas se definen
antes de correr nada y no se ajustan según el resultado. Si el número no sube, se
reporta como salió.

## 1. Validación walk-forward (origen móvil)

Reemplaza la división única por una evaluación de origen móvil, que es el método
estándar para series cortas (Tashman, 2000; Hyndman & Athanasopoulos, *Forecasting:
Principles and Practice*, sección "evaluation on a rolling forecasting origin").

Procedimiento, por cada origen `t`:

1. Ajustar el modelo usando únicamente los días `1..t` (el modelo base preentrenado
   con Kaggle no se vuelve a entrenar; solo se rehace el ajuste fino).
2. Pronosticar los días `t+1 .. t+H`, con `H` = horizonte (ver punto 2).
3. Comparar contra la demanda real de esos días.
4. Avanzar `t` en 1 día y repetir.

Origen inicial: el primer `t` que deje al menos `MIN_DIAS_ENTRENAMIENTO` días de
ajuste. Origen final: el último `t` tal que `t+H` no pase del último día con datos.

Requisitos:
- Ningún día del tramo pronosticado puede haber entrado en el ajuste de ese origen.
  Escribir un test que lo verifique explícitamente.
- Las tres categorías de un mismo día van siempre juntas (partir por fecha, no por
  fila), igual que ya hace `optimizar_modelo` con `TimeSeriesSplit`.
- Reportar cuántos orígenes se evaluaron. Si salen menos de 5, decirlo en la salida:
  la métrica sigue siendo frágil.

## 2. Horizonte de pronóstico: la ventana de lead time

Evaluar en dos horizontes y reportar los dos por separado:

- **Diario (H = 1):** lo que ya existe. Se mantiene para comparación.
- **Ventana de lead time (H = 7):** se suma la demanda pronosticada de los 7 días y se
  compara contra la suma de la demanda real de esos mismos 7 días.

El valor 7 debe leerse del lead time configurado en el sistema, no escribirse a mano.
Si hay lead times distintos por producto o proveedor, usar el que ya emplea el motor
de decisiones para calcular el stock de seguridad, y dejar documentado cuál es.

Para el horizonte de ventana el pronóstico se encadena: cada día pronosticado alimenta
las variables del siguiente, como hace `predecir_demanda` en la práctica. No usar la
demanda real de los días intermedios.

## 3. Métricas

Las mismas que ya existen, calculadas sobre las ventanas en lugar de sobre los días:

- WAPE y exactitud (1 − WAPE), por categoría y total.
- Acierto dentro de tolerancia relativa, con el mismo 20 % ya fijado.
- R² dentro de cada categoría.
- MAE y RMSE.

Agregar además la **dispersión entre orígenes**: desviación estándar del WAPE a lo
largo de los orígenes evaluados. Un WAPE de 30 % ± 4 no es lo mismo que 30 % ± 18, y
esa diferencia es lo que hay que reportar en la tesis.

## 4. Líneas base

Las mismas tres de la tarea anterior, evaluadas con el mismo procedimiento walk-forward
y el mismo horizonte:

1. Media por categoría de los días de ajuste de ese origen.
2. Media móvil de 7 días.
3. Último valor.

El modelo solo puede presentarse como aportando valor si supera a las tres en el
horizonte de ventana, que es el que importa para la decisión.

## 5. Comando

Extender `evaluar_predicciones` con `--horizonte` (`diario`, `ventana`, `ambos`;
por defecto `ambos`) y `--walk-forward` (por defecto activado, con la opción de
volver a la división única para reproducir resultados anteriores).

Salida: una tabla por horizonte, con las categorías y las líneas base al lado, más la
línea de dispersión entre orígenes.

Guardar en `artefactos/evaluacion/` con fecha, igual que ahora.

Respetar `--origen` (real por defecto). No tocar los datos. No activar ni registrar
ningún modelo.

## 6. Tests

En `inventario/tests/test_evaluacion.py`:

- Ningún día pronosticado aparece en el ajuste de su propio origen (test explícito,
  construyendo un caso donde la fuga sería detectable).
- El número de orígenes es el esperado dado el histórico, `MIN_DIAS_ENTRENAMIENTO` y `H`.
- La agregación por ventana suma correctamente: caso a mano con valores conocidos.
- El pronóstico de ventana encadena sus propias predicciones y no usa la demanda real
  de los días intermedios: alterar la demanda real de un día intermedio no debe cambiar
  el pronóstico de la ventana.
- WAPE de ventana ≠ promedio de los WAPE diarios (caso construido donde difieren).
- La dispersión entre orígenes se calcula sobre los orígenes, no sobre las filas.
- Con histórico insuficiente para un solo origen, el comando falla con un mensaje claro
  en vez de devolver un número.
- El comando no activa ni registra modelos.

## 7. Documentación

En el README, ampliar la sección "Exactitud del pronóstico" con:

- por qué el horizonte relevante es la ventana de lead time y no el día (ROP = d̄·L + SS);
- qué es la validación de origen móvil y por qué sustituye a la división única con
  series cortas, con la referencia bibliográfica;
- las tablas de ambos horizontes con las líneas base;
- la dispersión entre orígenes.

Actualizar también CLAUDE.md.

## Entregable

Pegar las dos tablas (horizonte diario y horizonte ventana), con las líneas base y la
dispersión. Decir explícitamente si el modelo supera o no a las tres líneas base en el
horizonte de ventana.

Si no las supera, no modificar nada para que las supere: ese es un resultado válido y
se reporta como salió.
