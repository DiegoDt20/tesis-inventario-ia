# Tarea 4: estudio de ablación — ¿cuánto aporta cada fuente de datos?

## Pregunta que responde

El asesor pregunta si el modelo predictivo funciona sin los datos de la empresa (solo
con el dataset externo de Kaggle) y cuánto cambia el resultado al incorporar el
pretest de agosto.

Esto se responde aislando las dos fuentes y midiéndolas por separado contra la misma
referencia.

## Precisión que debe quedar documentada

"Sin datos de la empresa" no es posible en sentido estricto: aunque el modelo no se
entrene con agosto, para pronosticar necesita los rezagos y medias móviles de los días
previos de la propia serie, que vienen de agosto. No se puede pronosticar una serie sin
su historial.

La configuración correcta se llama **"sin ajuste con datos de la empresa"**: el modelo
base no se reentrena con agosto, pero recibe el historial de agosto como entrada.
Escribirlo de otro modo sería incorrecto.

## 1. Configuraciones a evaluar

Las tres, sobre el **mismo walk-forward, los mismos orígenes y los mismos horizontes**
que ya usa `evaluar_predicciones`:

**(A) Solo externo.** Modelo base #1 entrenado únicamente con Kaggle. No se ajusta con
agosto. Recibe las features construidas con el historial de agosto hasta cada origen.

**(B) Solo interno.** XGBoost entrenado únicamente con los días de agosto hasta cada
origen, sin preentrenamiento con Kaggle. Misma configuración de hiperparámetros que el
ajuste fino del #6, para que la única diferencia sea la ausencia de transferencia.

**(C) Externo + interno.** El modelo vigente #6: base de Kaggle, ajuste fino con agosto
y escalado de nivel. Es el que ya está medido.

Mantener además las tres líneas base que ya existen (media por categoría, media móvil
de 7 días, último valor), para que la tabla quede completa.

## 2. El escalado

El escalado de nivel forma parte del procedimiento de ajuste fino del #6. Para (A) y (B)
hay que decidir y documentar antes de correr:

- **(A) Solo externo:** evaluar **con y sin** escalado, porque el sesgo diagnosticado
  nace justamente del modelo base. Es la medición que demuestra el piso del base sobre
  datos reales, no solo sobre la curva de respuesta sintética.
- **(B) Solo interno:** sin escalado. No hay dataset externo cuyo nivel haya que igualar,
  así que el escalado no tiene sentido ahí. Documentarlo.

Fijar esto antes de correr y no cambiarlo según el resultado.

## 3. Métricas

Las mismas que ya están implementadas, por categoría y total, en ambos horizontes
(diario y ventana de lead time):

- WAPE y exactitud (1 − WAPE)
- Acierto dentro de la tolerancia relativa del 20 %
- R² dentro de cada categoría y R² global
- Dispersión del WAPE entre orígenes
- Sesgo con signo por categoría (sobreestimación o subestimación)

## 4. Comparación por origen

Para cada par relevante, el conteo origen por origen con prueba de signos, como ya se
hace:

- C contra A → cuánto aporta el ajuste con datos de la empresa
- C contra B → cuánto aporta la transferencia desde Kaggle
- A contra B → cuál de las dos fuentes sola rinde mejor

Repetir la advertencia que ya está documentada: los orígenes se solapan, así que los
valores p son indicativos de consistencia y no prueba de significancia.

## 5. Entregable

Una tabla única con las seis filas (A, A con escalado, B, C y las tres líneas base) por
horizonte, y la tabla de comparaciones por origen.

Y una lectura explícita en la salida, en una línea por pregunta:

1. ¿Funciona el modelo sin ajuste con datos de la empresa? (configuración A)
2. ¿Funciona sin la transferencia desde Kaggle? (configuración B)
3. ¿La combinación supera a cada fuente por separado? (C contra A y contra B)

Si alguna respuesta es negativa —por ejemplo, si A rinde igual que C— decirlo tal cual.
Es un resultado válido y cambia lo que la tesis puede afirmar sobre la transferencia.

## 6. Lo que no hay que hacer

- No ajustar hiperparámetros de (B) para que compita mejor o peor. Misma configuración
  que el ajuste fino de (C).
- No cambiar umbrales, horizonte ni tolerancia.
- No activar ni registrar modelos nuevos. Esta tarea solo mide.
- No tocar los datos. Origen real, periodo agosto.

## 7. Tests

- (A) no entrena con ningún día de agosto: test que altera la demanda de los días de
  ajuste y verifica que las predicciones de (A) no cambian.
- (B) no usa el modelo base: test que verifica que el artefacto del base no se carga.
- (A), (B) y (C) se evalúan exactamente sobre los mismos orígenes y las mismas filas.
- El escalado se aplica solo donde se declaró.

## 8. Documentación

Sección nueva en el README, "Estudio de ablación", con las tablas, la precisión sobre
qué significa "sin ajuste con datos de la empresa", y la lectura de las tres preguntas.
Actualizar CLAUDE.md. Guardar el reporte en JSON con fecha.
