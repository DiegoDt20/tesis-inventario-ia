"""Pantalla de análisis para la tesis: composición del catálogo, demanda
por producto, motivos de pedidos no atendidos y comparación de los tres
indicadores entre periodos. Uso analítico/de investigación, separado del
dashboard operativo del día a día — ver views/dashboard.py."""
import json
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.db.models import Count, Sum
from django.shortcuts import render

from ..models import Categoria, PedidoDetalle, Producto
from ..servicios.indicadores import calcular_coi, calcular_ei, calcular_ns, serie_mensual
from ._comunes import _origen_o_defecto, _variacion_periodo, resolver_rango_periodo


def _distribucion_categorias():
    """Cuántos productos activos hay por categoría comercial
    (Producto.categoria), de mayor a menor. Es una lectura de composición
    del catálogo (qué vende la tienda), no un indicador de la tesis — así
    que, a diferencia de EI/NS/COI, no se filtra por origen: "prueba"/"real"
    separa datos de la metodología pretest/postest (movimientos, pedidos,
    conteos), pero el catálogo de productos es uno solo, completo."""
    qs = Producto.objects.filter(activo=True)
    filas = list(qs.values('categoria').annotate(total=Count('id')).order_by('-total'))
    total_general = sum(f['total'] for f in filas) or 1
    etiquetas = dict(Categoria.choices)
    for f in filas:
        f['etiqueta'] = etiquetas.get(f['categoria'], f['categoria'])
        f['porcentaje'] = f['total'] / total_general * 100
    return {'total': total_general, 'filas': filas}


def _top_productos_demanda(fecha_inicio, fecha_fin, origen, limite=7):
    """Productos con más unidades solicitadas en el rango (suma de
    PedidoDetalle.cantidad_solicitada, la misma fuente que usa el motor de
    predicción para la demanda real)."""
    qs = PedidoDetalle.objects.filter(
        pedido__fecha_solicitud__date__gte=fecha_inicio,
        pedido__fecha_solicitud__date__lte=fecha_fin,
    )
    if origen:
        qs = qs.filter(pedido__origen=origen)

    filas = list(
        qs.values('producto__codigo', 'producto__nombre')
        .annotate(total=Sum('cantidad_solicitada'))
        .order_by('-total')[:limite]
    )
    maximo = filas[0]['total'] if filas else 0
    for f in filas:
        f['porcentaje'] = (f['total'] / maximo * 100) if maximo else 0
    return filas


def _motivos_no_atencion(fecha_inicio, fecha_fin, origen):
    """Por qué no se atendieron completas las líneas de pedido del rango
    (PedidoDetalle.motivo_no_atencion), de más a menos frecuente. Explica
    el NS del periodo en vez de solo mostrar el porcentaje."""
    qs = PedidoDetalle.objects.filter(
        motivo_no_atencion__isnull=False,
        pedido__fecha_solicitud__date__gte=fecha_inicio,
        pedido__fecha_solicitud__date__lte=fecha_fin,
    )
    if origen:
        qs = qs.filter(pedido__origen=origen)

    filas = list(qs.values('motivo_no_atencion').annotate(total=Count('id')).order_by('-total'))
    total = sum(f['total'] for f in filas)
    if not total:
        return None

    etiquetas = dict(PedidoDetalle.MotivoNoAtencion.choices)
    for f in filas:
        f['etiqueta'] = etiquetas.get(f['motivo_no_atencion'], f['motivo_no_atencion'])
        f['porcentaje'] = f['total'] / total * 100
    return {'total': total, 'filas': filas}


@login_required
def reportes(request):
    origen = _origen_o_defecto(request)
    fecha_inicio, fecha_fin, rango = resolver_rango_periodo(request, origen)

    ei = calcular_ei(fecha_inicio, fecha_fin, origen=origen)
    ns = calcular_ns(fecha_inicio, fecha_fin, origen=origen)
    coi = calcular_coi(fecha_inicio, fecha_fin, origen=origen)
    evolucion = serie_mensual(fecha_inicio, fecha_fin, origen=origen)

    # Periodo anterior: misma duración, inmediatamente antes del rango
    # mostrado, para la comparación EI/NS/COI contra el periodo anterior.
    duracion_dias = (fecha_fin - fecha_inicio).days + 1
    fecha_fin_anterior = fecha_inicio - timedelta(days=1)
    fecha_inicio_anterior = fecha_fin_anterior - timedelta(days=duracion_dias - 1)
    ei_anterior = calcular_ei(fecha_inicio_anterior, fecha_fin_anterior, origen=origen)
    ns_anterior = calcular_ns(fecha_inicio_anterior, fecha_fin_anterior, origen=origen)
    coi_anterior = calcular_coi(fecha_inicio_anterior, fecha_fin_anterior, origen=origen)

    contexto = {
        'fecha_inicio': fecha_inicio,
        'fecha_fin': fecha_fin,
        'rango_activo': rango,
        'ei': ei,
        'ns': ns,
        'coi': coi,
        'ei_anterior': ei_anterior,
        'ns_anterior': ns_anterior,
        'coi_anterior': coi_anterior,
        'comparacion_ei': _variacion_periodo(ei['valor'], ei_anterior['valor'], mejor_si_sube=True),
        'comparacion_ns': _variacion_periodo(ns['valor'], ns_anterior['valor'], mejor_si_sube=True),
        'comparacion_coi': _variacion_periodo(coi['valor'], coi_anterior['valor'], mejor_si_sube=False),
        # Con menos de tres meses en el rango, la línea de tiempo apenas
        # tiene puntos que unir; reportes muestra en su lugar una
        # comparación directa contra el periodo anterior (ver plantilla).
        'meses_evolucion': len(evolucion),
        'evolucion_json': json.dumps(evolucion),
        'distribucion_categorias': _distribucion_categorias(),
        'top_productos': _top_productos_demanda(fecha_inicio, fecha_fin, origen),
        'motivos_no_atencion': _motivos_no_atencion(fecha_inicio, fecha_fin, origen),
    }
    plantilla = (
        'inventario/_reportes_resultados.html' if request.headers.get('HX-Request') == 'true'
        else 'inventario/reportes.html'
    )
    return render(request, plantilla, contexto)
