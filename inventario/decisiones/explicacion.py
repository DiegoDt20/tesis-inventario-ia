"""Explicación en lenguaje natural de una recomendación de reposición.

Son puras plantillas de texto con los números que ya calculó calculos.py:
NO usa ningún LLM. Más adelante un LLM podrá redactar esto de forma más
natural, pero los números siempre vendrán de aquí, nunca del LLM.
"""

NOTA_LEAD_TIME_REAL = 'calculado con el promedio real de compras recibidas'
NOTA_LEAD_TIME_PRODUCTO = (
    'configurado en la ficha del producto, porque aún no hay suficientes '
    'compras recibidas para calcular uno real'
)


def generar_explicacion(
    estado, cantidad_sugerida, stock_actual, punto_reorden, demanda_diaria,
    lead_time, stock_seguridad, nivel_servicio_objetivo, lead_time_es_real,
):
    """Arma el texto de la recomendación. `estado` debe ser uno de los
    valores de Recomendacion.Estado."""
    nivel_pct = round(nivel_servicio_objetivo * 100)
    nota_lead_time = NOTA_LEAD_TIME_REAL if lead_time_es_real else NOTA_LEAD_TIME_PRODUCTO

    contexto = (
        f'Se espera una demanda de {demanda_diaria:.1f} unidades diarias y el '
        f'proveedor tarda {lead_time:.0f} días ({nota_lead_time}). El stock de '
        f'seguridad calculado es de {stock_seguridad:.0f} unidades para un nivel '
        f'de servicio objetivo del {nivel_pct}%.'
    )

    if estado == 'critico':
        cabecera = (
            f'Stock crítico: el stock actual ({stock_actual:.0f}) está por '
            f'debajo del stock de seguridad ({stock_seguridad:.0f}). Se sugiere '
            f'pedir {cantidad_sugerida:.0f} unidades de inmediato.'
        )
    elif estado == 'reponer':
        cabecera = (
            f'Se sugiere pedir {cantidad_sugerida:.0f} unidades. El stock '
            f'actual ({stock_actual:.0f}) está por debajo del punto de reorden '
            f'({punto_reorden:.0f}).'
        )
    elif estado == 'exceso':
        cabecera = (
            f'El stock actual ({stock_actual:.0f}) supera varias veces el '
            f'punto de reorden ({punto_reorden:.0f}): hay sobrestock, lo que '
            f'genera costos de almacenamiento innecesarios. No se sugiere '
            f'pedir por ahora.'
        )
    else:  # normal
        cabecera = (
            f'El stock actual ({stock_actual:.0f}) está por encima del punto '
            f'de reorden ({punto_reorden:.0f}). No se necesita pedir por ahora.'
        )

    return f'{cabecera} {contexto}'
