"""Cálculo de los tres indicadores de la investigación (EI, NS, COI).

Funciones puras que leen de la base de datos y devuelven los valores para un
rango de fechas dado. Tanto el dashboard como el comando "cargar_datos" usan
estas mismas funciones, para que nunca haya dos cálculos distintos del mismo
indicador.
"""
from datetime import timedelta
from decimal import Decimal

from django.db.models import F, Max, Min, Sum

from ..models import ConteoDetalle, ConteoFisico, CostoAlmacenamiento, Origen, Pedido, PedidoDetalle


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
    qs = PedidoDetalle.objects.all()
    if origen:
        qs = qs.filter(pedido__origen=origen)
    if fecha_inicio:
        qs = qs.filter(pedido__fecha_solicitud__date__gte=fecha_inicio)
    if fecha_fin:
        qs = qs.filter(pedido__fecha_solicitud__date__lte=fecha_fin)

    total = qs.count()
    a_tiempo = qs.filter(atendido_a_tiempo=True).count()
    valor = (a_tiempo / total * 100) if total else None
    return {'valor': valor, 'a_tiempo': a_tiempo, 'total': total}


def calcular_coi(fecha_inicio=None, fecha_fin=None, origen=None):
    """Costos operativos de inventario:

        COI = costos de almacenamiento + pérdidas por desabastecimiento

    Costos de almacenamiento: suma de CostoAlmacenamiento.monto en el rango
    (filtrado por periodo_mes). Pérdidas por desabastecimiento: para cada
    PedidoDetalle no atendido por completo en el rango (filtrado por
    Pedido.fecha_solicitud), (cantidad no atendida) * margen unitario de la
    línea (precio_venta_unitario - costo_compra_unitario). Son el precio y
    el costo congelados al registrar el pedido, NO los actuales del
    Producto: así el COI de un periodo ya medido (el pretest) no cambia
    cuando después se actualizan precios.

    Devuelve {'valor': Decimal, 'almacenamiento': Decimal,
    'desabastecimiento': Decimal, 'tiene_datos': bool}. valor siempre es un
    Decimal (0.00 si no hay nada que sumar): a diferencia de EI/NS, "cero
    costos" es una suma válida, no una ausencia de medición. Para
    distinguir "no hubo datos este periodo" se usa 'tiene_datos'.
    """
    costos_qs = CostoAlmacenamiento.objects.all()
    if origen:
        costos_qs = costos_qs.filter(origen=origen)
    if fecha_inicio:
        costos_qs = costos_qs.filter(periodo_mes__gte=fecha_inicio)
    if fecha_fin:
        costos_qs = costos_qs.filter(periodo_mes__lte=fecha_fin)
    almacenamiento = costos_qs.aggregate(total=Sum('monto'))['total'] or Decimal('0.00')

    pedidos_qs = PedidoDetalle.objects.all()
    if origen:
        pedidos_qs = pedidos_qs.filter(pedido__origen=origen)
    if fecha_inicio:
        pedidos_qs = pedidos_qs.filter(pedido__fecha_solicitud__date__gte=fecha_inicio)
    if fecha_fin:
        pedidos_qs = pedidos_qs.filter(pedido__fecha_solicitud__date__lte=fecha_fin)

    detalles_qs = pedidos_qs.filter(
        cantidad_atendida__lt=F('cantidad_solicitada'),
    ).values_list('cantidad_solicitada', 'cantidad_atendida', 'precio_venta_unitario', 'costo_compra_unitario')

    desabastecimiento = sum(
        (
            (solicitada - atendida) * (precio_venta - costo_compra)
            for solicitada, atendida, precio_venta, costo_compra in detalles_qs
        ),
        Decimal('0.00'),
    )

    return {
        'valor': almacenamiento + desabastecimiento,
        'almacenamiento': almacenamiento,
        'desabastecimiento': desabastecimiento,
        # Si no hubo ni costos de almacenamiento ni pedidos ese periodo, no
        # hubo nada que medir (distinto de haber medido y dar cero).
        'tiene_datos': costos_qs.exists() or pedidos_qs.exists(),
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


def hay_mezcla_de_origenes():
    """True si hay datos de origen "prueba" Y de origen "real" al mismo
    tiempo en alguna de las fuentes que alimentan los indicadores
    (ConteoFisico para EI, Pedido para NS, CostoAlmacenamiento para COI).

    Sirve para advertir que un cálculo sin filtrar por origen estaría
    mezclando datos de prueba (pretest/postest) con datos reales, lo que no
    tiene sentido para la investigación."""
    modelos = (ConteoFisico, Pedido, CostoAlmacenamiento)
    tiene_prueba = any(modelo.objects.filter(origen=Origen.PRUEBA).exists() for modelo in modelos)
    tiene_real = any(modelo.objects.filter(origen=Origen.REAL).exists() for modelo in modelos)
    return tiene_prueba and tiene_real
