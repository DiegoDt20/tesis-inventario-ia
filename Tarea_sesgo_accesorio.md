# Tarea 3: diagnóstico y corrección del sesgo de la categoría accesorio

## Hecho que motiva la tarea

En la evaluación walk-forward sobre ventana de lead time (11 orígenes), el modelo
vigente pronostica **978 unidades de accesorio contra 589 reales**: sobreestima un
66 %. Las otras dos categorías no muestran ese patrón (esmalte 3.095 vs 2.933,
látex 2.067 vs 2.523).

Es un sesgo sistemático, no dispersión: el error va casi siempre en la misma
dirección. Tiene consecuencia operativa real, porque el motor de decisiones usa ese
pronóstico en ROP = d̄·L + SS y está recomendando comprar accesorios de más.

## Regla que no se negocia

Esta tarea es de diagnóstico, no de optimización. **Primero hay que identificar la
causa; solo después se corrige esa causa.** No se prueban variantes hasta que el WAPE
baje: eso sería ajustar el modelo al test, que es justo lo que `optimizar_modelo` ya
mostró que no funciona.

Si el diagnóstico no identifica una causa clara, el entregable es el diagnóstico y la
recomendación de no tocar nada. Eso es un resultado válido.

## 1. Caracterizar el sesgo

Antes de buscar causas, medir con precisión qué tipo de sesgo es:

- Error medio con signo (no absoluto) por categoría y por origen. ¿En cuántos de los
  11 orígenes el error de accesorio es positivo?
- ¿El sesgo aparece ya en el día 1 de la ventana, o se acumula al encadenar los 7 días?
  Medir el error con signo por posición dentro de la ventana (día 1, día 2 … día 7).
  Si crece con la posición, el problema está en el encadenamiento.
- ¿Cómo se comporta en el horizonte diario (H = 1, sin encadenar)? Ahí accesorio
  pronostica 208,5 contra 121 reales, así que el sesgo ya existe sin encadenamiento,
  pero hay que confirmar la proporción.

## 2. Hipótesis a descartar, en este orden

Cada una se comprueba con una medición, no con una opinión.

**(a) El sesgo viene del modelo base (Kaggle), no del ajuste fino.**
Comparar, solo para accesorio, las predicciones del modelo base #1 sin ajustar contra
las del modelo #5 ajustado, sobre los mismos orígenes. Si el base ya sobreestima en
proporción parecida, la transferencia está arrastrando la escala del dataset externo,
cuya demanda por serie es mucho mayor que la de accesorio.

**(b) El recorte en 0 introduce asimetría.**
`predecir_demanda` recorta las predicciones negativas a 0. En una serie con muchos días
en cero, el recorte solo puede empujar el promedio hacia arriba, nunca hacia abajo.
Medir cuántas predicciones de accesorio se recortaron y cuánto suma ese recorte.
Comparar el sesgo con y sin recorte (solo para el diagnóstico; el recorte no se quita
del sistema, porque una demanda negativa no tiene sentido).

**(c) Las variables de media y desviación móvil se degradan con muchos ceros.**
Medir la proporción de días con demanda cero en accesorio dentro del periodo, y
comparar con esmalte y látex. Si accesorio tiene muchos más ceros, las medias móviles
sobre ventanas con ceros no representan la demanda típica.

**(d) La categoría mezcla productos de rotación muy distinta.**
Accesorio agrupa lijas, rodillos, brochas y bases. Revisar si unos pocos productos
concentran la demanda y el resto está casi siempre en cero. Reportar la distribución,
sin cambiar la categorización.

## 3. Corrección, condicionada al diagnóstico

Solo si (a), (b), (c) o (d) queda identificada como causa dominante, aplicar la
corrección que le corresponde, y únicamente esa:

- Si es (a): evaluar escalar o normalizar la serie antes del ajuste fino, de modo que
  la transferencia aporte la forma temporal y no el nivel. Documentar que esto cambia
  el procedimiento de entrenamiento y hay que reflejarlo en la metodología.
- Si es (b): dejar el recorte, pero reportar el sesgo que introduce como limitación.
- Si es (c): considerar excluir accesorio del pronóstico por modelo y tratarla solo
  por punto de reorden, como ya se hace con base, solvente y temple por tener menos de
  15 días con venta. Sería aplicar un criterio existente, no uno nuevo.
- Si es (d): reportarlo como limitación de la agrupación por categoría, sin
  recategorizar productos (eso requiere confirmación del dueño).

Cualquier cambio se evalúa con el mismo walk-forward y las mismas líneas base, y se
reporta el antes y el después de las tres categorías, no solo de accesorio.

## 4. Lo que no hay que hacer

- No probar configuraciones de hiperparámetros buscando que el número baje.
- No excluir accesorio del reporte para que el total suba.
- No cambiar el umbral del 20 % ni el horizonte.
- No recategorizar productos sin que el dueño lo confirme.
- No tocar los datos. Origen real, periodo agosto.
- No activar ni registrar modelos.

## 5. Tests

- El cálculo del error con signo distingue sobreestimación de subestimación (caso a mano).
- El error por posición dentro de la ventana se calcula sobre la posición, no sobre la fecha.
- El conteo de predicciones recortadas en 0 es correcto (caso construido).
- Si se aplica una corrección, un test que verifique que el procedimiento de
  entrenamiento cambió solo donde se dijo y que las otras categorías siguen igual.

## 6. Entregable

Un informe corto en la salida del comando, con:

1. La caracterización del sesgo (signo, orígenes afectados, evolución dentro de la ventana).
2. Qué hipótesis quedó identificada y con qué medición.
3. Si se aplicó corrección: el antes y el después de las tres categorías, con las
   líneas base al lado.
4. Si no se aplicó: por qué, y qué haría falta para decidirlo.

Actualizar el README y CLAUDE.md con lo que se encuentre, en la sección de exactitud
del pronóstico.
