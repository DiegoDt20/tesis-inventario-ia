# Tarea 8 (revisada): resumen visual de /modelo/, con poco texto

## Problema

Dos cosas a la vez:

1. Con el detalle plegado, la página queda medio vacía: el contenido termina a media
   pantalla.
2. El resumen es casi todo texto. Cuatro bloques de párrafos donde debería haber
   indicadores visuales.

La solución **no** es abrir el detalle por defecto. Es convertir el resumen en una
pantalla de indicadores, con el mínimo de palabras, y agregar un gráfico que llene la
parte baja con información útil.

Los datos y los umbrales no cambian. Solo cambia cómo se presentan.

## 1. Recortar el texto al mínimo

Lo que hay ahora y qué pasa con cada cosa:

| Texto actual | Qué hacer |
|---|---|
| "Exactitud del pronóstico" | Se queda, como etiqueta pequeña sobre el número. |
| "De cada 100 unidades que se venden en una semana, el pronóstico acierta casi 84; las otras 16 se le escapan, de más o de menos." | **Eliminar por completo.** El porcentaje se muestra solo, sin frase que lo explique. |
| "El sistema usa este pronóstico para calcular cuánto reponer durante los 7 días que tarda en llegar un pedido al proveedor." | Sacarla del cuerpo. Va detrás de un icono de información (tooltip o ayuda emergente) junto al número. El dato es necesario para interpretar bien, pero no tiene que ocupar dos renglones. |
| "Verde desde 80 %, ámbar entre 60 % y 80 %, rojo por debajo de 60 %." | Igual: detrás de un icono de información junto a las barras, no como texto suelto. |
| La advertencia de accesorio | Se queda visible, pero más corta: "Accesorio: conviene revisar la sugerencia antes de comprar." Mantener el icono de alerta y el color ámbar. |

Nada más de texto en el resumen. Queda únicamente: la etiqueta "Exactitud del
pronóstico", el porcentaje total, los tres porcentajes por categoría con sus barras, y
la línea de advertencia de accesorio. Ninguna frase explicativa acompaña a los números.

## 2. El número, como indicador

"83,6 %" debe ser el elemento dominante de la pantalla: tipografía grande, el porcentaje
como protagonista, la etiqueta pequeña encima.

Acompañarlo con un indicador circular (anillo de progreso) que muestre el 83,6 % sobre
100. Mismo lenguaje visual que los indicadores que ya usa el dashboard; no introducir un
estilo nuevo.

## 3. Las categorías, como barras

Reemplazar las tres líneas de texto por tres barras horizontales:

- Nombre de la categoría a la izquierda, barra al centro, porcentaje a la derecha.
- Barra llena hasta su porcentaje sobre una pista del 100 %.
- Color según el semáforo ya definido (verde desde 80 %, ámbar 60–80 %, rojo debajo).
- Que se vea de un golpe que accesorio queda por detrás.

Mismos nombres, mismos valores, mismos umbrales.

## 4. Gráfico de pronóstico contra venta real

Es lo que falta y lo que llena la parte baja con algo que sirve: ver que la línea del
pronóstico sigue a la real a lo largo de las once ventanas evaluadas.

- Dos series por categoría: demanda real y pronosticada, sumadas por ventana de 7 días.
- **Un gráfico por categoría**, o uno con selector de categoría. No juntar las tres en el
  mismo eje: esmalte mueve ~270 unidades por ventana y accesorio ~54, así que la chica
  quedaría aplastada.
- Datos leídos del reporte JSON de evaluación, no recalculados al cargar. Si el reporte
  no trae el detalle por ventana, agregarlo en `evaluar_predicciones`.
- Eje vertical en unidades desde cero; eje horizontal, la fecha de inicio de cada ventana.
- Series distinguibles sin depender solo del color (leyenda clara o etiqueta directa).
- Sin efectos tridimensionales ni degradados. Líneas o barras agrupadas.
- Legible en tema claro y oscuro.
- Debajo, una línea corta diciendo qué se ve. Una línea, no un párrafo.

Usar la misma librería de gráficos que ya emplea el dashboard.

## 5. La barra del detalle técnico

Hoy parece un separador. Debe verse como una sección propia: más altura, icono, título y
descripción en dos renglones, y una señal clara de que se despliega.

Sigue **cerrada por defecto**.

## 6. Composición

Con el anillo, las barras y el gráfico, el resumen debe ocupar la pantalla de forma
natural. Revisar a 1366×768 y a ancho de portátil pequeño.

Si al final sobra espacio, dejarlo. El espacio en blanco ordenado se ve mejor que un
elemento agregado para rellenar.

## Lo que no hay que hacer

- No abrir el detalle técnico por defecto.
- No mover contenido del detalle al resumen.
- No cambiar números, umbrales ni los nombres de las categorías.
- No quitar la advertencia de accesorio: solo acortarla.
- No esconder detrás de un icono algo que haga falta para no malinterpretar la cifra
  principal; el contexto del lead time y los umbrales sí pueden ir ahí porque el número
  se entiende sin ellos.
- No agregar una librería de gráficos nueva.
- No inventar datos: si una ventana no está en el reporte, no se dibuja.

## Tests

- El gráfico se alimenta del reporte, no de cálculos en la vista.
- Sin reporte, el resumen muestra el aviso y no un gráfico vacío.
- Los valores dibujados coinciden con el reporte en al menos una ventana verificada a mano.
- Las barras aplican el color correcto en los tres casos del semáforo.
- La advertencia de accesorio sigue presente.
- El detalle técnico sigue cerrado por defecto.

## Entregable

Capturas del resumen en tema claro y oscuro, tal como se ve al abrir la página.
Actualizar README y CLAUDE.md.
