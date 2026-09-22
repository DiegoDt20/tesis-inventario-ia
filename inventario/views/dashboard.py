"""Pantalla de indicadores (para la tesis): EI, NS, COI, estado del modelo
de predicción activo, recomendaciones urgentes y anomalías sin revisar."""
import json
from datetime import date, timedelta

from django.contrib.auth.decorators import login_required
from django.db.models import Case, Count, IntegerField, Max, Min, Sum, When
from django.shortcuts import render

from ..models import Anomalia, Categoria, ModeloEntrenado, PedidoDetalle, Producto, Recomendacion
from ..servicios.indicadores import (
    calcular_coi,
    calcular_ei,
    calcular_ns,
    hay_mezcla_de_origenes,
    rango_disponible,
    serie_mensual,
)
from ._comunes import _parsear_fecha, _parsear_origen
from .anomalias import _anomalias_no_revisadas


def _variacion_periodo(valor_actual, valor_anterior, mejor_si_sube=True):
    """Compara un indicador contra el mismo periodo anterior (misma
    duración, inmediatamente antes del rango mostrado). Devuelve None si no
    hay con qué comparar (falta alguno de los dos valores, o el anterior es
    0 y no se puede calcular un porcentaje). Si hay comparación, devuelve
    un dict con la variación en porcentaje (siempre positiva: la dirección
    va aparte) y si esa variación es una mejora o un empeoramiento — EI y
    NS mejoran subiendo, COI mejora bajando, así que la misma flecha hacia
    arriba es buena para uno y mala para otro."""
    if valor_actual is None or valor_anterior is None or valor_anterior == 0:
        return None
    variacion = ((valor_actual - valor_anterior) / abs(valor_anterior)) * 100
    if variacion == 0:
        return {'variacion': 0.0, 'sube': None, 'mejora': None}
    sube = variacion > 0
    return {'variacion': abs(variacion), 'sube': sube, 'mejora': sube == mejor_si_sube}


def _estado_modelo_activo(origen):
    """Panel de estado del modelo activo: prioriza el ajustado (transferencia
    ya aplicada) y cae al base si todavía no hay uno ajustado."""
    modelo = (
        ModeloEntrenado.objects.filter(fase=ModeloEntrenado.Fase.AJUSTADO, activo=True).first()
        or ModeloEntrenado.objects.filter(fase=ModeloEntrenado.Fase.BASE, activo=True).first()
    )

    detalles_qs = PedidoDetalle.objects.all()
    if origen:
        detalles_qs = detalles_qs.filter(pedido__origen=origen)
    rango = detalles_qs.aggregate(
        minimo=Min('pedido__fecha_solicitud'), maximo=Max('pedido__fecha_solicitud'),
    )
    dias_historico = None
    if rango['minimo'] and rango['maximo']:
        dias_historico = (rango['maximo'].date() - rango['minimo'].date()).days + 1

    # Para no inducir a error: las métricas de un modelo "base" se
    # midieron sobre el dataset externo de Kaggle, no sobre la
    # microempresa; solo las de un modelo "ajustado" son sobre datos
    # internos reales.
    fuente_metricas = None
    if modelo and modelo.fase == ModeloEntrenado.Fase.BASE:
        fuente_metricas = (
            'Estas métricas se midieron sobre el dataset externo de Kaggle '
            '(Store Item Demand Forecasting Challenge), no sobre datos de la microempresa.'
        )
    elif modelo and modelo.fase == ModeloEntrenado.Fase.AJUSTADO:
        if modelo.origen_datos_internos:
            fuente_metricas = (
                'Estas métricas se midieron sobre los datos internos de la microempresa '
                f'(origen: {modelo.get_origen_datos_internos_display()}).'
            )
        else:
            fuente_metricas = 'Estas métricas se midieron sobre los datos internos de la microempresa.'

    return {'modelo': modelo, 'dias_historico': dias_historico, 'fuente_metricas': fuente_metricas}


def _recomendaciones_urgentes(origen):
    """Productos en estado crítico o para reponer, del último lote generado
    por "generar_recomendaciones", ordenados por urgencia (crítico primero)
    y luego por cantidad sugerida descendente."""
    ultima_fecha_generacion = (
        Recomendacion.objects.order_by('-fecha_generacion').values_list('fecha_generacion', flat=True).first()
    )
    if ultima_fecha_generacion is None:
        return []

    orden_urgencia = Case(
        When(estado=Recomendacion.Estado.CRITICO, then=0),
        When(estado=Recomendacion.Estado.REPONER, then=1),
        default=2,
        output_field=IntegerField(),
    )
    qs = Recomendacion.objects.filter(
        fecha_generacion=ultima_fecha_generacion,
        estado__in=[Recomendacion.Estado.CRITICO, Recomendacion.Estado.REPONER],
    )
    if origen:
        qs = qs.filter(producto__origen=origen)
    return list(
        qs.select_related('producto').annotate(orden_urgencia=orden_urgencia)
        .order_by('orden_urgencia', '-cantidad_sugerida')
    )


def _distribucion_categorias(origen):
    """Cuántos productos activos hay por categoría comercial (Producto.categoria),
    de mayor a menor, para el panel "catálogo por categoría" del dashboard.
    Es una lectura de composición del catálogo, no un indicador de la tesis."""
    qs = Producto.objects.filter(activo=True)
    if origen:
        qs = qs.filter(origen=origen)
    filas = list(qs.values('categoria').annotate(total=Count('id')).order_by('-total'))
    total_general = sum(f['total'] for f in filas) or 1
    etiquetas = dict(Categoria.choices)
    for f in filas:
        f['etiqueta'] = etiquetas.get(f['categoria'], f['categoria'])
        f['porcentaje'] = f['total'] / total_general * 100
    return {'total': total_general, 'filas': filas}


def _salud_catalogo(origen):
    """Cuántos productos del último lote de recomendaciones están en cada
    estado (crítico/reponer/normal/exceso), en ese orden fijo (de más a
    menos urgente), para la barra apilada "salud del catálogo" del
    dashboard. Mismos estados y mismos colores que la lista de
    recomendaciones, solo que aquí se ve la proporción del catálogo
    completo en vez de la lista uno por uno."""
    ultima_fecha_generacion = (
        Recomendacion.objects.order_by('-fecha_generacion').values_list('fecha_generacion', flat=True).first()
    )
    if ultima_fecha_generacion is None:
        return None

    qs = Recomendacion.objects.filter(fecha_generacion=ultima_fecha_generacion)
    if origen:
        qs = qs.filter(producto__origen=origen)

    conteos = {estado: 0 for estado in Recomendacion.Estado.values}
    for fila in qs.values('estado').annotate(total=Count('id')):
        conteos[fila['estado']] = fila['total']
    total = sum(conteos.values())
    if not total:
        return None

    orden = [
        Recomendacion.Estado.CRITICO, Recomendacion.Estado.REPONER,
        Recomendacion.Estado.NORMAL, Recomendacion.Estado.EXCESO,
    ]
    segmentos = [
        {
            'estado': estado,
            'etiqueta': Recomendacion.Estado(estado).label,
            'total': conteos[estado],
            'porcentaje': conteos[estado] / total * 100,
        }
        for estado in orden if conteos[estado]
    ]
    return {'fecha_generacion': ultima_fecha_generacion, 'total': total, 'segmentos': segmentos}


def _anomalias_por_severidad(origen):
    """Cuántas anomalías sin revisar hay por severidad (alta/media/baja),
    para la barra apilada del dashboard. Mismo dato que
    _anomalias_no_revisadas, solo que agregado en vez de listado."""
    qs = Anomalia.objects.filter(revisada=False)
    if origen:
        qs = qs.filter(producto__origen=origen)

    conteos = {severidad: 0 for severidad in Anomalia.Severidad.values}
    for fila in qs.values('severidad').annotate(total=Count('id')):
        conteos[fila['severidad']] = fila['total']
    total = sum(conteos.values())
    if not total:
        return None

    orden = [Anomalia.Severidad.ALTA, Anomalia.Severidad.MEDIA, Anomalia.Severidad.BAJA]
    segmentos = [
        {
            'severidad': severidad,
            'etiqueta': Anomalia.Severidad(severidad).label,
            'total': conteos[severidad],
            'porcentaje': conteos[severidad] / total * 100,
        }
        for severidad in orden if conteos[severidad]
    ]
    return {'total': total, 'segmentos': segmentos}


def _top_productos_demanda(fecha_inicio, fecha_fin, origen, limite=7):
    """Productos con más unidades solicitadas en el rango (suma de
    PedidoDetalle.cantidad_solicitada, la misma fuente que usa el motor de
    predicción para la demanda real), para el ranking del dashboard."""
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
def dashboard(request):
    origen = _parsear_origen(request.GET.get('origen'))

    # Por defecto, el rango cubre TODO lo que hay cargado (no una ventana
    # arbitraria de últimos N días, que podía dejar pedidos reales fuera y
    # mostrar un NS incompleto). Si no hay datos todavía, se usa hoy.
    fecha_min_disponible, fecha_max_disponible = rango_disponible(origen=origen)
    hoy = date.today()
    fecha_fin = _parsear_fecha(request.GET.get('fecha_fin')) or fecha_max_disponible or hoy
    fecha_inicio = _parsear_fecha(request.GET.get('fecha_inicio')) or fecha_min_disponible or fecha_fin
    if fecha_inicio > fecha_fin:
        fecha_inicio, fecha_fin = fecha_fin, fecha_inicio

    ei = calcular_ei(fecha_inicio, fecha_fin, origen=origen)
    ns = calcular_ns(fecha_inicio, fecha_fin, origen=origen)
    coi = calcular_coi(fecha_inicio, fecha_fin, origen=origen)
    evolucion = serie_mensual(fecha_inicio, fecha_fin, origen=origen)

    # Periodo anterior: misma duración, inmediatamente antes del rango
    # mostrado, para poder decir "subió/bajó X% contra el periodo anterior"
    # en las tarjetas de indicadores.
    duracion_dias = (fecha_fin - fecha_inicio).days + 1
    fecha_fin_anterior = fecha_inicio - timedelta(days=1)
    fecha_inicio_anterior = fecha_fin_anterior - timedelta(days=duracion_dias - 1)
    ei_anterior = calcular_ei(fecha_inicio_anterior, fecha_fin_anterior, origen=origen)
    ns_anterior = calcular_ns(fecha_inicio_anterior, fecha_fin_anterior, origen=origen)
    coi_anterior = calcular_coi(fecha_inicio_anterior, fecha_fin_anterior, origen=origen)

    # Solo tiene sentido advertir cuando no se filtró por un origen
    # puntual: si ya se eligió "Prueba" o "Real", los números no están
    # mezclados sin importar qué más haya en la base de datos.
    advertencia_mezcla = origen is None and hay_mezcla_de_origenes()

    # El dashboard es para verlo de un vistazo, no para scrollear un
    # listado completo: muestra solo los primeros casos (ya vienen
    # ordenados por urgencia/severidad) y enlaza a la pantalla con el
    # listado paginado completo para el resto.
    LIMITE_PREVIA = 8
    recomendaciones_todas = _recomendaciones_urgentes(origen)
    anomalias_todas = _anomalias_no_revisadas(origen)

    contexto = {
        'fecha_inicio': fecha_inicio,
        'fecha_fin': fecha_fin,
        'origen': origen or '',
        'advertencia_mezcla': advertencia_mezcla,
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
        # tiene puntos que unir; el dashboard muestra en su lugar una
        # comparación directa contra el periodo anterior (ver plantilla).
        'meses_evolucion': len(evolucion),
        'evolucion_json': json.dumps(evolucion),
        'recomendaciones': recomendaciones_todas[:LIMITE_PREVIA],
        'recomendaciones_total': len(recomendaciones_todas),
        'estado_modelo': _estado_modelo_activo(origen),
        'anomalias': anomalias_todas[:LIMITE_PREVIA],
        'anomalias_total': len(anomalias_todas),
        'salud_catalogo': _salud_catalogo(origen),
        'anomalias_severidad': _anomalias_por_severidad(origen),
        'distribucion_categorias': _distribucion_categorias(origen),
        'top_productos': _top_productos_demanda(fecha_inicio, fecha_fin, origen),
        'motivos_no_atencion': _motivos_no_atencion(fecha_inicio, fecha_fin, origen),
    }
    # El formulario de filtros pide esta misma URL por HTMX y solo necesita
    # la zona de resultados: la plantilla completa (con sidebar, etc.) sería
    # trabajo de red y de parseo desperdiciado en cada filtro.
    plantilla = (
        'inventario/_dashboard_resultados.html' if request.headers.get('HX-Request') == 'true'
        else 'inventario/dashboard.html'
    )
    return render(request, plantilla, contexto)
