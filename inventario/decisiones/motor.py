"""Motor de decisiones de reposición: para cada producto activo combina la
demanda predicha (Prediccion, generada por el motor de ML) con el historial
real de demanda y de compras para calcular stock de seguridad, punto de
reorden, cantidad sugerida y el estado del producto.

Lógica determinística: nada de aprendizaje automático aquí, solo las
fórmulas de calculos.py.
"""
from django.db.models import F, Sum

from inventario.ml.carga_interna import leer_demanda_interna
from inventario.ml.features import construir_features
from inventario.models import Compra, CompraDetalle, NivelPrediccion, Prediccion, Recomendacion

from .calculos import cantidad_a_pedir, punto_reorden, stock_seguridad
from .explicacion import generar_explicacion

# Bajo este número de compras recibidas con fecha de llegada conocida no se
# confía en el lead time real: se usa producto.lead_time_dias en su lugar.
MIN_COMPRAS_PARA_LEAD_TIME_REAL = 3

# A partir de cuántas veces el punto de reorden se considera sobrestock.
UMBRAL_EXCESO = 3


def calcular_desviaciones_demanda():
    """Desviación estándar de la demanda histórica REAL (PedidoDetalle
    .cantidad_solicitada), por producto. Usa la misma densificación diaria
    que el motor de ML (días sin pedidos cuentan como demanda cero) para
    que la variabilidad no salga subestimada por ignorar esos días.

    Devuelve un dict {producto_id: desviacion}. Se calcula una sola vez
    para todos los productos porque recorre todo PedidoDetalle.
    """
    df_interno = leer_demanda_interna()
    if df_interno.empty:
        return {}
    df_features = construir_features(df_interno)
    desviaciones = df_features.groupby('serie_id')['cantidad'].std(ddof=1)
    return desviaciones.fillna(0.0).to_dict()


def _lead_time_real(producto):
    """Promedio de (fecha de llegada - fecha de pedido de la compra) de las
    líneas de compra de este producto que ya se recibieron, solo si hay
    MIN_COMPRAS_PARA_LEAD_TIME_REAL o más con una fecha de llegada
    calculable. Si no hay suficientes, devuelve None."""
    lineas = CompraDetalle.objects.filter(
        producto=producto, cantidad_recibida__gt=0,
    ).select_related('compra')

    dias_por_linea = []
    for linea in lineas:
        fecha_llegada = linea.fecha_llegada_efectiva
        if fecha_llegada and linea.compra.fecha_pedido:
            dias_por_linea.append((fecha_llegada - linea.compra.fecha_pedido).days)

    if len(dias_por_linea) < MIN_COMPRAS_PARA_LEAD_TIME_REAL:
        return None
    return sum(dias_por_linea) / len(dias_por_linea)


def _pedidos_en_transito(producto):
    """Unidades ya pedidas a proveedores pero aún no recibidas (compras que
    no están canceladas)."""
    total = CompraDetalle.objects.filter(producto=producto).exclude(
        compra__estado=Compra.Estado.CANCELADA,
    ).aggregate(total=Sum(F('cantidad_pedida') - F('cantidad_recibida')))['total']
    return total or 0


def _demanda_predicha_periodo(producto, dias_cobertura):
    """Suma de la demanda predicha para los próximos `dias_cobertura` días,
    tomando el lote de predicciones más reciente de este producto (todas
    las que comparten la misma fecha_generacion). Devuelve un dict con
    'demanda_periodo', 'demanda_diaria' (promedio), 'dias_horizonte' y,
    si la predicción fue por categoría, 'demanda_categoria_periodo' y
    'participacion_usada' (None si fue por producto); o None si el
    producto no tiene predicciones."""
    ultima_generacion = Prediccion.objects.filter(producto=producto).order_by(
        '-fecha_generacion'
    ).values_list('fecha_generacion', flat=True).first()
    if ultima_generacion is None:
        return None

    predicciones = list(
        Prediccion.objects.filter(producto=producto, fecha_generacion=ultima_generacion)
        .order_by('fecha_objetivo')
        .values('fecha_objetivo', 'demanda_predicha', 'nivel_prediccion', 'participacion_usada')[:dias_cobertura]
    )
    if not predicciones:
        return None

    demanda_periodo = sum(p['demanda_predicha'] for p in predicciones)
    demanda_categoria_periodo = participacion_usada = None
    if predicciones[0]['nivel_prediccion'] == NivelPrediccion.CATEGORIA:
        participacion_usada = predicciones[0]['participacion_usada']
        # Suma de lo repartido a todos los productos de la categoría en las
        # mismas fechas: es la predicción de la categoría completa (las
        # participaciones suman 1), y no depende de que la del producto
        # sea distinta de cero para poder despejarla.
        demanda_categoria_periodo = Prediccion.objects.filter(
            fecha_generacion=ultima_generacion,
            producto__categoria=producto.categoria,
            fecha_objetivo__in=[p['fecha_objetivo'] for p in predicciones],
        ).aggregate(total=Sum('demanda_predicha'))['total']

    return {
        'demanda_periodo': demanda_periodo,
        'demanda_diaria': demanda_periodo / len(predicciones),
        'dias_horizonte': len(predicciones),
        'demanda_categoria_periodo': demanda_categoria_periodo,
        'participacion_usada': participacion_usada,
    }


def _determinar_estado(stock_actual, stock_seguridad_valor, punto_reorden_valor):
    # <= y no <: si el stock está justo en el límite del stock de seguridad
    # (o en 0 cuando SS también sale en 0, p. ej. un producto sin historial
    # de demanda todavía) ya no queda margen, así que cuenta como crítico.
    if stock_actual <= stock_seguridad_valor:
        return Recomendacion.Estado.CRITICO
    if stock_actual < punto_reorden_valor:
        return Recomendacion.Estado.REPONER
    if punto_reorden_valor > 0 and stock_actual > punto_reorden_valor * UMBRAL_EXCESO:
        return Recomendacion.Estado.EXCESO
    return Recomendacion.Estado.NORMAL


def calcular_recomendacion(producto, nivel_servicio_objetivo, dias_cobertura, desviaciones=None):
    """Calcula todos los números para un producto y devuelve un dict listo
    para `Recomendacion.objects.create(producto=producto, **dict)`, o None
    si el producto no tiene predicciones de demanda todavía."""
    demanda = _demanda_predicha_periodo(producto, dias_cobertura)
    if demanda is None:
        return None
    demanda_periodo = demanda['demanda_periodo']
    demanda_diaria = demanda['demanda_diaria']

    if desviaciones is None:
        desviaciones = calcular_desviaciones_demanda()
    desviacion_demanda = float(desviaciones.get(producto.pk, 0.0))

    lead_time_real = _lead_time_real(producto)
    lead_time_es_real = lead_time_real is not None
    lead_time_usado = lead_time_real if lead_time_es_real else float(producto.lead_time_dias)

    ss = stock_seguridad(desviacion_demanda, lead_time_usado, nivel_servicio_objetivo)
    rop = punto_reorden(demanda_diaria, lead_time_usado, ss)
    en_transito = _pedidos_en_transito(producto)
    cantidad = cantidad_a_pedir(demanda_periodo, producto.stock_actual, ss, en_transito)

    estado = _determinar_estado(producto.stock_actual, ss, rop)

    explicacion = generar_explicacion(
        estado=estado,
        cantidad_sugerida=cantidad,
        stock_actual=producto.stock_actual,
        punto_reorden=rop,
        demanda_diaria=demanda_diaria,
        lead_time=lead_time_usado,
        stock_seguridad=ss,
        nivel_servicio_objetivo=nivel_servicio_objetivo,
        lead_time_es_real=lead_time_es_real,
    )

    return {
        'estado': estado,
        'stock_actual_snapshot': producto.stock_actual,
        'demanda_predicha_periodo': demanda_periodo,
        'desviacion_demanda': desviacion_demanda,
        'lead_time_usado': lead_time_usado,
        'stock_seguridad': ss,
        'punto_reorden': rop,
        'cantidad_sugerida': cantidad,
        'nivel_servicio_objetivo': nivel_servicio_objetivo,
        'dias_horizonte': demanda['dias_horizonte'],
        'demanda_categoria_periodo': demanda['demanda_categoria_periodo'],
        'participacion_usada': demanda['participacion_usada'],
        'lead_time_es_real': lead_time_es_real,
        'pedidos_en_transito': en_transito,
        'explicacion': explicacion,
    }
