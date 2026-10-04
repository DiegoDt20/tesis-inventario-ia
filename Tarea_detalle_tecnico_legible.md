# Tarea 7: hacer legible el detalle técnico de /modelo/

## Problema

El detalle técnico tiene todos los números correctos pero ninguna explicación. Es una
sucesión de tablas sin relato: no dice qué pregunta responde cada una ni qué se concluye
de ella. El propio autor de la tesis no puede leerla de corrido, y es la sección que
tendrá que explicar en la sustentación.

No hay que quitar datos. Hay que agregar texto alrededor y eliminar repeticiones.

## Criterio de aceptación

Una persona que conozca el negocio pero no sepa de aprendizaje automático debe poder
leer la sección de arriba abajo y, al terminar, poder decir con sus palabras: qué se
midió, cómo se midió, contra qué se comparó, qué salió y qué no se puede afirmar.

## 1. Párrafo de orientación al inicio de la sección

Antes de la primera tabla, tres o cuatro frases que expliquen el método completo:

- Que el pronóstico se evalúa repitiendo el entrenamiento muchas veces: se entrena con
  los primeros días, se pronostican los siguientes, se avanza un día y se repite. Cada
  repetición es un "origen". Así se obtienen once mediciones independientes del mismo
  mes en lugar de una sola.
- Que se mide sobre la ventana de 7 días porque es lo que tarda en llegar un pedido al
  proveedor, y es la cantidad que hay que acertar para decidir cuánto comprar.
- Que todo se compara contra tres métodos simples sin inteligencia artificial, porque un
  modelo solo sirve si le gana a lo obvio.

Sin fórmulas. Si hace falta nombrar WAPE o R², que se nombren después, en el glosario.

## 2. Cada bloque con pregunta y conclusión

Cada tabla queda envuelta en tres cosas:

**Un encabezado en forma de pregunta**, en lugar del título técnico actual. Por ejemplo
"¿Qué tan exacto es el pronóstico?" en lugar de "Exactitud del pronóstico del modelo en
producción"; "¿Le gana a métodos simples?"; "¿Cuánto aporta cada fuente de datos?".

**Una frase antes de la tabla** diciendo qué se está midiendo y sobre qué datos.

**Una frase después de la tabla** con la conclusión que se saca de ella, redactada como
la diría una persona. Por ejemplo, bajo la tabla de líneas base: "El modelo gana a los
tres métodos simples, y lo hace de forma consistente: en 9, 10 y 10 de los 11 orígenes.
La diferencia es chica frente a la media por categoría, así que el modelo aporta, pero
no de forma espectacular."

Las conclusiones se redactan a partir de los números del reporte, no escritas a mano:
si un número cambia al reevaluar, la frase debe seguir siendo cierta o debe recalcularse.
Si eso resulta complejo para alguna frase, generar la parte variable (los conteos, los
porcentajes) desde el reporte y dejar fija solo la estructura de la oración.

## 3. Glosario corto

Un bloque desplegable, "¿Qué significan estos términos?", con definiciones de una línea:

- **Exactitud** — qué porcentaje de las unidades vendidas acierta el pronóstico. 84 %
  significa que de cada 100 unidades se desvía en 16.
- **Origen** — cada una de las repeticiones del entrenamiento. Once orígenes son once
  mediciones distintas del mismo mes.
- **Línea base** — un método simple sin inteligencia artificial, usado como referencia.
  Si el modelo no le gana, no se justifica.
- **Ablación** — quitar una pieza del diseño y volver a medir, para saber cuánto aportaba
  esa pieza.
- **Prueba de signos** — cuenta en cuántos orígenes gana el modelo, para distinguir una
  ventaja consistente de una casualidad.
- **R²** — qué parte de la variación diaria explica el modelo. Aquí se reporta dentro de
  cada categoría porque el global engaña.

Las definiciones deben coincidir con cómo se usan los términos en la página.

## 4. Quitar repeticiones

Hoy la advertencia del R² global aparece dos veces y la del solapamiento de orígenes tres
o cuatro. Dejar cada una **una sola vez**, en el lugar donde primero hace falta, y no
repetirla más abajo.

## 5. Jerarquía visual

El horizonte diario y el historial de modelos son material de respaldo: que se distingan
visualmente de las dos secciones principales (exactitud y líneas base), con un tamaño de
encabezado menor o un fondo distinto. Que al recorrer la sección se note qué es lo
importante y qué es el anexo.

## Lo que no hay que hacer

- No borrar tablas, columnas ni advertencias. Solo reorganizar y agregar texto.
- No cambiar ningún número.
- No agregar jerga nueva. Si una palabra necesita explicación, va al glosario.
- No tocar el resumen de arriba, que ya quedó bien.

## Tests

- Cada bloque principal tiene su encabezado en forma de pregunta y su frase de conclusión.
- Las frases de conclusión contienen los valores del reporte y cambian si el reporte cambia.
- Cada advertencia aparece exactamente una vez en la página renderizada.
- El glosario está presente y cerrado por defecto.

## Entregable

Captura de la sección técnica abierta, de arriba abajo. Actualizar README y CLAUDE.md.
