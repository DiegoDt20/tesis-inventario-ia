# Tarea 6: resumen en castellano al inicio de /modelo/, detalle técnico plegado

## Problema

La página quedó correcta para sustentar la tesis y mala para usar el sistema. El dueño
de la microempresa la abre y ve seis tablas con WAPE, R² intra-categoría, desviación
entre orígenes y pruebas de signos. No puede sacar de ahí lo único que le importa:
si puede confiar o no en lo que el sistema le sugiere comprar.

No hay que borrar nada. Hay que **anteponer un resumen y plegar el detalle**.

## 1. Bloque de resumen, arriba de todo

Lo primero de la página, antes de las tarjetas de modelos. Sin jerga: nada de WAPE, R²,
valores p, orígenes ni desviaciones en este bloque.

**(a) Un número grande: 83,6 %** (leído del reporte, no escrito a mano), con la etiqueta
"Exactitud del pronóstico".

Debajo, una sola frase que lo explique en castellano llano, del estilo:
"De cada 100 unidades que se venden en una semana, el pronóstico acierta casi 84."

**(b) Para qué sirve**, una línea:
"El sistema usa este pronóstico para calcular cuánto reponer durante los 7 días que
tarda en llegar un pedido al proveedor."

**(c) Las tres categorías**, como tres indicadores simples con semáforo:

| Categoría | Exactitud | Color |
|---|---|---|
| Esmalte | 86,8 % | verde |
| Látex | 83,7 % | verde |
| Accesorios | 67,2 % | ámbar |

Los umbrales de color se fijan ahora y se documentan: verde desde 80 %, ámbar entre
60 y 80 %, rojo por debajo de 60 %. No ajustarlos después según el resultado.

**(d) Una advertencia honesta**, en una línea, para que el dueño sepa dónde no confiar:
"En accesorios el pronóstico es menos confiable. Conviene revisar esa sugerencia antes
de comprar."

Ese bloque completo tiene que caber en una pantalla sin desplazarse y leerse en menos
de diez segundos.

## 2. Plegar todo el detalle técnico

Todo lo demás —tarjetas de modelos, métricas por horizonte, líneas base, prueba de
signos, ablación, historial— va dentro de una sección plegable con un encabezado del
estilo:

"Detalle técnico de la investigación" + una línea pequeña: "Evaluación completa,
comparación con métodos simples y estudio de ablación. Pensado para la documentación de
la tesis."

**Cerrada por defecto.** Que se abra con un clic y que el estado no se recuerde entre
visitas: cada vez que se entra, cerrada.

El contenido de adentro no cambia. Está bien como está.

## 3. El horizonte diario

Dentro del detalle, no en el resumen. La exactitud diaria es 48,8 % y mostrarla arriba
junto al 83,6 % sin explicación confundiría al dueño: pensaría que el sistema falla la
mitad de las veces.

Dejarlo donde está, con su etiqueta de "secundario", y que el resumen hable solo de la
ventana de reposición, que es el horizonte sobre el que se decide la compra.

## 4. Coherencia

El número del resumen y el de la tabla de ventana tienen que salir de la misma fuente
(el reporte JSON) y ser el mismo valor. Si no hay reporte, el resumen dice que falta
generar la evaluación y nombra el comando, en lugar de mostrar un número vacío o viejo.

## Lo que no hay que hacer

- No borrar ninguna tabla ni advertencia de las que ya están.
- No redondear el número a algo distinto de lo que dice el reporte.
- No quitar la advertencia de accesorios para que el resumen se vea mejor.
- No recalcular métricas al cargar.

## Tests

- El resumen muestra el mismo valor que la tabla de ventana, leído del mismo reporte.
- Los umbrales de color aplican correctamente en los tres casos (verde, ámbar, rojo).
- La sección técnica está cerrada por defecto en el HTML renderizado.
- Sin reporte disponible, el resumen muestra el aviso y el comando, no un número.
- La advertencia de accesorios está presente.

## Entregable

Captura del resumen tal como se ve al abrir la página, con el detalle cerrado.
Actualizar README y CLAUDE.md.
