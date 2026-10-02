"""Motor de decisiones de reposición: para cada producto activo combina la
demanda predicha (Prediccion, generada por el motor de ML) con el historial
real de demanda y de compras para calcular stock de seguridad, punto de
reorden, cantidad sugerida y el estado del producto.

Un producto sin pronóstico en el lote vigente (su categoría no alcanza los
días con venta que exige el modelo, ver carga_interna.MIN_DIAS_VENTA_CATEGORIA)
no se omite: se gestiona solo con punto de reorden y stock mínimo, con la
demanda diaria promedio real en lugar de la pronosticada
(Recomendacion.Metodo.PUNTO_REORDEN).

Lógica determinística: nada de aprendizaje automático aquí, solo las
fórmulas de calculos.py.
"""
from datetime import timedelta

import pandas as pd
from django.db.models import F, Sum

from inventario.ml.carga_interna import leer_demanda_interna
from inventario.models import Compra, CompraDetalle, NivelPrediccion, Origen, Prediccion, Recomendacion

from .calculos import cantidad_a_pedir, punto_reorden, stock_seguridad
from .explicacion import generar_explicacion

# Bajo este número de compras recibidas con fecha de llegada conocida no se
# confía en el lead time real: se usa producto.lead_time_dias en su lugar.
MIN_COMPRAS_PARA_LEAD_TIME_REAL = 3

# A partir de cuántas veces el punto de reorden se considera sobrestock.
UMBRAL_EXCESO = 3


def calcular_estadisticas_demanda(origen=Origen.REAL):
    """Desviación estándar de la demanda histórica (PedidoDetalle
    .cantidad_solicitada) de los pedidos del `origen` dado ("real" por
    defecto), por producto. Nunca mezcla orígenes: sin este filtro los
    pedidos de prueba de septiembre entraban en la desviación que alimenta
    el stock de seguridad de todas las recomendaciones.

    La demanda diaria de cada producto se toma sobre TODOS los días del
    periodo con pedidos de ese origen (del primer al último día con algún
    pedido, de cualquier producto), con cero los días sin pedidos. No se usa
    la densificación del motor de ML (features._densificar), que rellena
    cada serie solo entre SU primer y SU último pedido: un producto pedido
    una sola vez quedaba como una serie de un día, con desviación 0 (stock
    de seguridad 0), cuando en realidad no se vendió los otros 30 días.

    Sobre la misma serie diaria calcula también la demanda diaria promedio
    (para los productos gestionados solo con punto de reorden) y el último
    día del periodo, la fecha de corte del histórico.

    Devuelve un dict con 'desviaciones' y 'promedios' ({producto_id:
    valor}; un producto sin pedidos en el periodo no aparece, 0 al usarlo)
    y 'fecha_corte' (date, o None sin pedidos). Se calcula una sola vez
    para todos los productos porque recorre todo PedidoDetalle.
    """
    df_interno = leer_demanda_interna(origen=origen)
    if df_interno.empty:
        return {'desviaciones': {}, 'promedios': {}, 'fecha_corte': None}
    dias_del_periodo = pd.date_range(df_interno['fecha'].min(), df_interno['fecha'].max(), freq='D')
    demanda_diaria = (
        df_interno.pivot_table(index='fecha', columns='serie_id', values='cantidad', aggfunc='sum')
        .reindex(dias_del_periodo)
        .fillna(0.0)
    )
    return {
        'desviaciones': demanda_diaria.std(ddof=1).fillna(0.0).to_dict(),
        'promedios': demanda_diaria.mean().to_dict(),
        'fecha_corte': dias_del_periodo[-1].date(),
    }


def calcular_desviaciones_demanda(origen=Origen.REAL):
    """Solo las desviaciones de calcular_estadisticas_demanda:
    {producto_id: desviacion}."""
    return calcular_estadisticas_demanda(origen)['desviaciones']


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


def lead_time_producto(producto):
    """Lead time (días) que usa el motor para este producto: el promedio
    real de sus compras recibidas si hay MIN_COMPRAS_PARA_LEAD_TIME_REAL o
    más; si no, el configurado en la ficha (producto.lead_time_dias).
    Devuelve (lead_time, es_real). Lo usan el stock de seguridad y la
    ventana de evaluación del pronóstico (evaluar_predicciones)."""
    lead_time_real = _lead_time_real(producto)
    if lead_time_real is not None:
        return lead_time_real, True
    return float(producto.lead_time_dias), False


def _pedidos_en_transito(producto):
    """Unidades ya pedidas a proveedores pero aún no recibidas (compras que
    no están canceladas)."""
    total = CompraDetalle.objects.filter(producto=producto).exclude(
        compra__estado=Compra.Estado.CANCELADA,
    ).aggregate(total=Sum(F('cantidad_pedida') - F('cantidad_recibida')))['total']
    return total or 0


def _demanda_predicha_periodo(producto, dias_cobertura, ultima_generacion):
    """Suma de la demanda predicha para los próximos `dias_cobertura` días,
    tomando las predicciones de este producto en el lote vigente
    (`ultima_generacion`, ver Prediccion.fecha_ultimo_lote). No se cae a un
    lote anterior del mismo producto: si no está en el vigente, no tiene
    pronóstico. Devuelve un dict con 'demanda_periodo', 'demanda_diaria'
    (promedio), 'dias_horizonte', 'fecha_corte' (el día antes de la primera
    fecha pronosticada) y, si la predicción fue por categoría,
    'demanda_categoria_periodo' y 'participacion_usada' (None si fue por
    producto); o None si el producto no tiene predicciones en ese lote."""
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
        'fecha_corte': predicciones[0]['fecha_objetivo'] - timedelta(days=1),
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


def calcular_recomendacion(
    producto, nivel_servicio_objetivo, dias_cobertura, estadisticas=None, origen=Origen.REAL,
    ultima_generacion=None,
):
    """Calcula todos los números para un producto y devuelve un dict listo
    para `Recomendacion.objects.create(producto=producto, **dict)`.

    Con pronóstico en el lote vigente (`ultima_generacion`; si no se pasa,
    Prediccion.fecha_ultimo_lote()) usa la demanda predicha. Sin él, usa el
    método de punto de reorden: la demanda diaria promedio real del
    histórico en lugar de la pronosticada, y el stock mínimo de la ficha
    como piso del punto de reorden (ver _calcular_por_punto_reorden)."""
    if estadisticas is None:
        estadisticas = calcular_estadisticas_demanda(origen)
    if ultima_generacion is None:
        ultima_generacion = Prediccion.fecha_ultimo_lote()
    desviacion_demanda = float(estadisticas['desviaciones'].get(producto.pk, 0.0))

    lead_time_usado, lead_time_es_real = lead_time_producto(producto)

    ss = stock_seguridad(desviacion_demanda, lead_time_usado, nivel_servicio_objetivo)
    en_transito = _pedidos_en_transito(producto)

    demanda = _demanda_predicha_periodo(producto, dias_cobertura, ultima_generacion)
    if demanda is not None:
        metodo = Recomendacion.Metodo.PRONOSTICO
        demanda_diaria = demanda['demanda_diaria']
        rop = punto_reorden(demanda_diaria, lead_time_usado, ss)
        cantidad = cantidad_a_pedir(demanda['demanda_periodo'], producto.stock_actual, ss, en_transito)
        fecha_corte = demanda['fecha_corte']
        stock_minimo = None
        datos_demanda = {
            'demanda_predicha_periodo': demanda['demanda_periodo'],
            'demanda_diaria_historica': None,
            'dias_horizonte': demanda['dias_horizonte'],
            'demanda_categoria_periodo': demanda['demanda_categoria_periodo'],
            'participacion_usada': demanda['participacion_usada'],
        }
    else:
        metodo = Recomendacion.Metodo.PUNTO_REORDEN
        demanda_diaria = float(estadisticas['promedios'].get(producto.pk, 0.0))
        stock_minimo = producto.stock_minimo
        rop, cantidad = _calcular_por_punto_reorden(
            demanda_diaria, lead_time_usado, ss, stock_minimo, dias_cobertura,
            producto.stock_actual, en_transito,
        )
        fecha_corte = estadisticas['fecha_corte']
        datos_demanda = {
            'demanda_predicha_periodo': None,
            'demanda_diaria_historica': demanda_diaria,
            'dias_horizonte': dias_cobertura,
            'demanda_categoria_periodo': None,
            'participacion_usada': None,
        }

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
        metodo=metodo,
        fecha_corte=fecha_corte,
        stock_minimo=stock_minimo,
    )

    return {
        'estado': estado,
        'metodo': metodo,
        'stock_actual_snapshot': producto.stock_actual,
        **datos_demanda,
        'stock_minimo_snapshot': stock_minimo,
        'fecha_corte_historico': fecha_corte,
        'desviacion_demanda': desviacion_demanda,
        'lead_time_usado': lead_time_usado,
        'stock_seguridad': ss,
        'punto_reorden': rop,
        'cantidad_sugerida': cantidad,
        'nivel_servicio_objetivo': nivel_servicio_objetivo,
        'lead_time_es_real': lead_time_es_real,
        'pedidos_en_transito': en_transito,
        'origen_demanda': origen,
        'explicacion': explicacion,
    }


def _calcular_por_punto_reorden(
    demanda_diaria_historica, lead_time, ss, stock_minimo, dias_cobertura, stock_actual, en_transito,
):
    """Punto de reorden y cantidad a pedir sin demanda pronosticada.

    - ROP = max(d_hist * L + SS, stock mínimo): el stock mínimo de la ficha
      es el piso, para que un producto con poca o ninguna venta registrada
      igual se reponga al bajar de ese nivel.
    - Q = max(0, max(d_hist * días de cobertura + SS, ROP) - stock - en
      tránsito): la misma política "order-up-to" que con pronóstico, con la
      demanda histórica en lugar de la predicha y sin quedar por debajo del
      punto de reorden.

    Devuelve (rop, cantidad)."""
    rop = max(punto_reorden(demanda_diaria_historica, lead_time, ss), float(stock_minimo))
    nivel_objetivo = max(demanda_diaria_historica * dias_cobertura + ss, rop)
    cantidad = max(nivel_objetivo - stock_actual - en_transito, 0.0)
    return rop, cantidad
