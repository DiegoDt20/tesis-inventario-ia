"""Cálculo de los tres indicadores de la investigación (EI, NS, COI).

Funciones puras que leen de la base de datos y devuelven los valores para un
rango de fechas dado. Tanto el dashboard como el comando "cargar_datos" usan
estas mismas funciones, para que nunca haya dos cálculos distintos del mismo
indicador.
"""
from datetime import timedelta
from decimal import Decimal

from django.db.models import F, Max, Min, Sum

from ..models import ConteoDetalle, ConteoFisico, CostoAlmacenamiento, Pedido, PedidoDetalle


def calcular_ei(fecha_inicio=None, fecha_fin=None, origen=None):
    """Exactitud del inventario:

        EI = (stock registrado correctamente / total de registros) * 100

    Se apoya en ConteoDetalle (comparación stock_sistema vs stock_fisico;
    diferencia = 0 significa registro correcto). El rango de fechas filtra
    por ConteoFisico.fecha_corte.

    Devuelve {'valor': float|None, 'correctos': int, 'total': int}.
    valor es None cuando no hay registros en el rango (no hay "0%" posible
    de calcular, solo ausencia de datos).
    """
    qs = ConteoDetalle.objects.all()
    if origen:
        qs = qs.filter(conteo__origen=origen)
    if fecha_inicio:
        qs = qs.filter(conteo__fecha_corte__gte=fecha_inicio)
    if fecha_fin:
        qs = qs.filter(conteo__fecha_corte__lte=fecha_fin)

    total = qs.count()
    correctos = qs.filter(stock_sistema=F('stock_fisico')).count()
    valor = (correctos / total * 100) if total else None
    return {'valor': valor, 'correctos': correctos, 'total': total}


def calcular_ns(fecha_inicio=None, fecha_fin=None, origen=None):
    """Nivel de servicio:

        NS = (pedidos atendidos a tiempo / pedidos totales) * 100

    Se apoya en PedidoDetalle (campo atendido_a_tiempo). El rango de fechas
    filtra por Pedido.fecha_solicitud.

    Devuelve {'valor': float|None, 'a_tiempo': int, 'total': int}.
    """
    qs = _pedidos_del_periodo(fecha_inicio, fecha_fin, origen)
    total = qs.count()
    a_tiempo = qs.filter(atendido_a_tiempo=True).count()
    valor = (a_tiempo / total * 100) if total else None
    return {'valor': valor, 'a_tiempo': a_tiempo, 'total': total}


def _pedidos_del_periodo(fecha_inicio=None, fecha_fin=None, origen=None):
    """PedidoDetalle del rango (por Pedido.fecha_solicitud) y del origen: la
    misma base para NS y para el bloque PD del COI."""
    qs = PedidoDetalle.objects.all()
    if origen:
        qs = qs.filter(pedido__origen=origen)
    if fecha_inicio:
        qs = qs.filter(pedido__fecha_solicitud__date__gte=fecha_inicio)
    if fecha_fin:
        qs = qs.filter(pedido__fecha_solicitud__date__lte=fecha_fin)
    return qs


def lineas_desabastecimiento(fecha_inicio=None, fecha_fin=None, origen=None):
    """Las líneas que entran en las pérdidas por desabastecimiento (PD) del
    COI: PedidoDetalle del rango con cantidad atendida menor que la
    solicitada. Es la misma lista que la hoja 3_COI_PD de la ficha, así que
    sirve para cuadrar el PD línea por línea (comando reporte_indicadores
    --detalle)."""
    return _pedidos_del_periodo(fecha_inicio, fecha_fin, origen).filter(
        cantidad_atendida__lt=F('cantidad_solicitada'),
    )


def calcular_coi(fecha_inicio=None, fecha_fin=None, origen=None):
    """Costos operativos de inventario:

        COI = CA + PD
        CA  = suma de CostoAlmacenamiento.monto del periodo
        PD  = Σ (cantidad solicitada − cantidad atendida) × margen unitario

    CA: costos de almacenamiento, filtrados por periodo_mes (un registro por
    concepto y mes; el modelo no guarda una ventana más fina que el mes).
    PD: pérdidas por desabastecimiento, una por cada PedidoDetalle no
    atendido por completo en el rango (filtrado por Pedido.fecha_solicitud),
    valorizada al margen unitario de la línea (precio_venta_unitario -
    costo_compra_unitario). Es la utilidad no percibida, no el precio de
    venta: si la venta se hubiera hecho, también se habría incurrido en el
    costo de compra (Silver, Pyke & Peterson; Chopra & Meindl). Son el
    precio y el costo congelados al registrar el pedido, NO los actuales
    del Producto: así el COI de un periodo ya medido (el pretest) no cambia
    cuando después se actualizan precios.

    Devuelve {'valor': Decimal, 'almacenamiento': Decimal,
    'desabastecimiento': Decimal, 'tiene_datos': bool} y el desglose para
    cuadrar contra la ficha: 'conceptos_almacenamiento' (lista de dicts
    periodo_mes/concepto/monto), 'lineas_desabastecimiento',
    'unidades_no_atendidas', 'productos_afectados' y
    'lineas_precio_reconstruido' (líneas del PD cuyo precio congelado es
    una reconstrucción, ver PedidoDetalle.precios_reconstruidos). valor
    siempre es un Decimal (0.00 si no hay nada que sumar): a diferencia de
    EI/NS, "cero costos" es una suma válida, no una ausencia de medición.
    Para distinguir "no hubo datos este periodo" se usa 'tiene_datos'.
    """
    costos_qs = CostoAlmacenamiento.objects.all()
    if origen:
        costos_qs = costos_qs.filter(origen=origen)
    if fecha_inicio:
        costos_qs = costos_qs.filter(periodo_mes__gte=fecha_inicio)
    if fecha_fin:
        costos_qs = costos_qs.filter(periodo_mes__lte=fecha_fin)
    conceptos = list(costos_qs.order_by('periodo_mes', 'pk').values('periodo_mes', 'concepto', 'monto'))
    almacenamiento = sum((c['monto'] for c in conceptos), Decimal('0.00'))

    pedidos_qs = _pedidos_del_periodo(fecha_inicio, fecha_fin, origen)
    detalles = list(lineas_desabastecimiento(fecha_inicio, fecha_fin, origen).values_list(
        'cantidad_solicitada', 'cantidad_atendida', 'precio_venta_unitario',
        'costo_compra_unitario', 'producto_id', 'precios_reconstruidos',
    ))

    desabastecimiento = sum(
        (
            (solicitada - atendida) * (precio_venta - costo_compra)
            for solicitada, atendida, precio_venta, costo_compra, _, _ in detalles
        ),
        Decimal('0.00'),
    )

    return {
        'valor': almacenamiento + desabastecimiento,
        'almacenamiento': almacenamiento,
        'desabastecimiento': desabastecimiento,
        # Si no hubo ni costos de almacenamiento ni pedidos ese periodo, no
        # hubo nada que medir (distinto de haber medido y dar cero).
        'tiene_datos': bool(conceptos) or pedidos_qs.exists(),
        'conceptos_almacenamiento': conceptos,
        'lineas_desabastecimiento': len(detalles),
        'unidades_no_atendidas': sum(d[0] - d[1] for d in detalles),
        'productos_afectados': len({d[4] for d in detalles}),
        'lineas_precio_reconstruido': sum(1 for d in detalles if d[5]),
    }


def _primer_dia_siguiente_mes(fecha):
    if fecha.month == 12:
        return fecha.replace(year=fecha.year + 1, month=1, day=1)
    return fecha.replace(month=fecha.month + 1, day=1)


def serie_mensual(fecha_inicio, fecha_fin, origen=None):
    """Evolución mensual de los tres indicadores entre fecha_inicio y
    fecha_fin (inclusive), un punto por mes calendario. Reutiliza
    calcular_ei/calcular_ns/calcular_coi para cada mes, para no duplicar
    la lógica de cálculo.

    Devuelve una lista de dicts: {'periodo': 'YYYY-MM', 'ei', 'ns', 'coi'}.
    Un mes sin datos queda en None (no en 0): un cero se leería como "el
    indicador dio cero ese mes", cuando en realidad no hubo medición.
    """
    puntos = []
    cursor = fecha_inicio.replace(day=1)
    while cursor <= fecha_fin:
        inicio_mes_siguiente = _primer_dia_siguiente_mes(cursor)
        fin_mes = min(inicio_mes_siguiente - timedelta(days=1), fecha_fin)

        ei = calcular_ei(cursor, fin_mes, origen=origen)
        ns = calcular_ns(cursor, fin_mes, origen=origen)
        coi = calcular_coi(cursor, fin_mes, origen=origen)

        puntos.append({
            'periodo': cursor.strftime('%Y-%m'),
            # ei['valor'] y ns['valor'] ya son None sin registros ese mes.
            'ei': ei['valor'],
            'ns': ns['valor'],
            'coi': float(coi['valor']) if coi['tiene_datos'] else None,
        })
        cursor = inicio_mes_siguiente

    return puntos


def rango_disponible(origen=None):
    """Fecha mínima y máxima entre TODOS los datos disponibles para los
    indicadores (ConteoFisico.fecha_corte, Pedido.fecha_solicitud,
    CostoAlmacenamiento.periodo_mes). Sirve para que el selector de fechas
    del dashboard pueda arrancar cubriendo todo lo que hay cargado, en vez
    de una ventana arbitraria que podría dejar datos reales fuera.

    Devuelve (fecha_minima, fecha_maxima) como date, o (None, None) si no
    hay ningún dato todavía.
    """
    conteos_qs = ConteoFisico.objects.all()
    pedidos_qs = Pedido.objects.all()
    costos_qs = CostoAlmacenamiento.objects.all()
    if origen:
        conteos_qs = conteos_qs.filter(origen=origen)
        pedidos_qs = pedidos_qs.filter(origen=origen)
        costos_qs = costos_qs.filter(origen=origen)

    rango_conteo = conteos_qs.aggregate(minimo=Min('fecha_corte'), maximo=Max('fecha_corte'))
    rango_pedido = pedidos_qs.aggregate(minimo=Min('fecha_solicitud'), maximo=Max('fecha_solicitud'))
    rango_costo = costos_qs.aggregate(minimo=Min('periodo_mes'), maximo=Max('periodo_mes'))

    fechas_min = [rango_conteo['minimo'], rango_costo['minimo']]
    fechas_max = [rango_conteo['maximo'], rango_costo['maximo']]
    if rango_pedido['minimo']:
        fechas_min.append(rango_pedido['minimo'].date())
        fechas_max.append(rango_pedido['maximo'].date())

    fechas_min = [f for f in fechas_min if f is not None]
    fechas_max = [f for f in fechas_max if f is not None]
    if not fechas_min:
        return None, None
    return min(fechas_min), max(fechas_max)

