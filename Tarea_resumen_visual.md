# Tarea 8: dar peso visual al resumen de /modelo/

## Problema

El resumen quedó correcto en contenido pero pobre en presentación: párrafos pequeños en
una tarjeta ancha, con mucho espacio sin usar. La página se ve vacía al abrirla.

La solución **no** es abrir el detalle técnico por defecto: eso devolvería el problema
que la tarea 6 resolvió. La solución es que el resumen tenga el peso visual que le
corresponde y que incluya una evidencia visual de la exactitud.

## 1. El número principal

Hoy "83,6 %" se ve del mismo tamaño que el texto que lo rodea. Debe ser el elemento
dominante de la pantalla: tipografía grande, el porcentaje como protagonista y la
etiqueta "Exactitud del pronóstico" encima en tamaño pequeño.

La frase explicativa ("De cada 100 unidades…") queda debajo, en texto normal.

## 2. Las categorías, como barras

Reemplazar las tres líneas de texto por tres barras horizontales con su porcentaje al
final y el color del semáforo ya definido (verde desde 80 %, ámbar 60–80 %, rojo
debajo). Que se vea de un golpe que accesorio está por detrás de las otras dos.

Mantener los nombres y los valores actuales. No cambiar los umbrales.

## 3. Gráfico de pronóstico contra venta real

Esto es lo que falta y lo que más vale para el dueño: ver que la línea del pronóstico
sigue a la real.

Un gráfico por cada una de las once ventanas evaluadas, mostrando para cada ventana la
demanda real y la pronosticada, sumadas por semana. Dos series: real y pronóstico.

Requisitos:

- **Un gráfico por categoría**, o uno solo con selector de categoría. Lo que resulte más
  simple de leer; no juntar las tres en el mismo eje, porque sus volúmenes son muy
  distintos (esmalte ~270 por ventana, accesorio ~54) y la categoría chica quedaría
  aplastada.
- Datos leídos del reporte JSON de evaluación, no recalculados al cargar. Si el reporte
  no trae el detalle por ventana, agregarlo al reporte en `evaluar_predicciones`.
- Etiquetar las series directamente sobre el gráfico o con una leyenda clara; que no
  dependa solo del color para distinguirlas.
- Eje vertical en unidades, empezando en cero. Eje horizontal: la fecha de inicio de
  cada ventana.
- Sin efectos tridimensionales, sin degradados, sin adornos. Líneas o barras agrupadas.
- Debe leerse igual en el tema claro y en el oscuro, que la página ya soporta.
- Una leyenda de una línea debajo: qué se está viendo, en castellano llano.

Usar la misma librería de gráficos que ya emplea el dashboard, para no agregar
dependencias.

## 4. La barra del detalle técnico

Hoy es una línea delgada que parece un separador. Debe verse como una sección propia:
más altura, un icono, el título y la descripción en dos renglones, y una indicación
clara de que se despliega.

Sigue **cerrada por defecto**. Eso no cambia.

## 5. Composición

Con el número grande, las barras y el gráfico, el resumen debe ocupar la pantalla de
forma natural, sin huecos grandes ni elementos estirados para rellenar. Revisar a
1366×768 y a ancho de portátil pequeño.

Si al final sobra espacio, dejarlo: el espacio en blanco ordenado se ve mejor que un
elemento agregado para rellenar.

## Lo que no hay que hacer

- No abrir el detalle técnico por defecto.
- No mover nada del detalle al resumen.
- No cambiar números, umbrales ni textos ya aprobados.
- No agregar una librería de gráficos nueva.
- No inventar datos para el gráfico: si una ventana no está en el reporte, no se dibuja.

## Tests

- El gráfico se alimenta del reporte y no de cálculos en la vista.
- Sin reporte, el resumen muestra el aviso y no un gráfico vacío.
- Los valores dibujados coinciden con los del reporte para al menos una ventana
  verificada a mano.
- El detalle técnico sigue cerrado por defecto.

## Entregable

Capturas del resumen en tema claro y oscuro, tal como se ve al abrir la página.
Actualizar README y CLAUDE.md.
