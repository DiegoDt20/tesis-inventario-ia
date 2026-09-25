"""Pantalla operativa del día a día (para el dueño del negocio): los tres
indicadores de la tesis, la predicción de demanda, la recomendación más
urgente, la salud del catálogo y lo que hoy pide atención (reposición,
anomalías). El uso técnico/de investigación (modelo de predicción, reportes
de composición y comparación entre periodos) vive aparte, en /modelo y
/reportes — ver views/modelo.py y views/reportes.py."""
import json
from datetime import date, timedelta

from django.contrib.auth.decorators import login_required
from django.db.models import Case, Count, IntegerField, Max, Sum, When
from django.shortcuts import render

from ..models import Anomalia, Categoria, PedidoDetalle, Prediccion, Producto, Recomendacion
from ..servicios.indicadores import calcular_coi, calcular_ei, calcular_ns
from ._comunes import _origen_o_defecto, _variacion_periodo, resolver_rango_periodo
from .anomalias import _anomalias_no_revisadas


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
    """Cuántas anomalías sin revisar hay por severidad (alta/media/baja):
    solo el conteo, para el resumen en la cabecera de la tabla de anomalías
    del dashboard (antes era además una barra apilada aparte, que duplicaba
    la misma información que ya muestra la columna "Severidad" de la
    tabla)."""
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
        {'severidad': severidad, 'etiqueta': Anomalia.Severidad(severidad).label, 'total': conteos[severidad]}
        for severidad in orden if conteos[severidad]
    ]
    return {'total': total, 'segmentos': segmentos}


def _productos_con_prediccion(origen):
    """Productos que tienen al menos una Prediccion guardada, para el
    selector del gráfico de predicción de demanda del dashboard."""
    qs = Producto.objects.filter(predicciones__isnull=False)
    if origen:
        qs = qs.filter(origen=origen)
    return list(qs.distinct().order_by('nombre'))


def _ultima_prediccion_por_producto(productos_ids):
    """{producto_id: fecha_generacion de su lote más reciente}, para no
    mezclar corridas de "predecir_demanda" distintas al sumar por
    categoría (cada producto aporta solo su propio último lote)."""
    return dict(
        Prediccion.objects.filter(producto_id__in=productos_ids)
        .values('producto_id').annotate(ultima=Max('fecha_generacion'))
        .values_list('producto_id', 'ultima')
    )


def _categorias_con_prediccion(origen):
    """Categorías con al menos un producto con predicción, con su demanda
    pronosticada total (sumando el último lote de cada producto que la
    compone — la misma agrupación por Producto.categoria que usa
    inventario/ml/carga_interna.py para entrenar por categoría cuando
    ningún producto por sí solo tiene histórico suficiente). Ordenadas de
    mayor a menor demanda: el selector del gráfico abre siempre con la
    primera de esta lista."""
    productos = _productos_con_prediccion(origen)
    if not productos:
        return []
    categoria_por_producto = {p.pk: p.categoria for p in productos}
    ids = list(categoria_por_producto)
    ultimas_por_producto = _ultima_prediccion_por_producto(ids)

    totales = {}
    filas = (
        Prediccion.objects.filter(producto_id__in=ids)
        .values('producto_id', 'fecha_generacion')
        .annotate(total=Sum('demanda_predicha'))
    )
    for fila in filas:
        if fila['fecha_generacion'] != ultimas_por_producto.get(fila['producto_id']):
            continue
        categoria = categoria_por_producto[fila['producto_id']]
        totales[categoria] = totales.get(categoria, 0.0) + fila['total']

    etiquetas = dict(Categoria.choices)
    filas_ordenadas = sorted(totales.items(), key=lambda par: par[1], reverse=True)
    return [
        {'valor': codigo, 'etiqueta': etiquetas.get(codigo, codigo), 'total_pronostico': total}
        for codigo, total in filas_ordenadas
    ]


def _elegir_seleccion_grafico(categorias, productos, seleccion):
    """Qué mostrar en el gráfico de predicción: lo pedido explícitamente
    por query string si es válido; si no, la categoría con más demanda
    pronosticada (el pronóstico se calcula por categoría — un producto
    suelto rara vez tiene historial entrenable, ver Categoria); si no hay
    ninguna categoría con predicción, el primer producto con una.
    Devuelve una tupla (tipo, valor) con tipo 'categoria'/'producto', o
    (None, None) si no hay nada que graficar."""
    if seleccion:
        tipo, _, valor = seleccion.partition(':')
        if tipo == 'categoria' and any(c['valor'] == valor for c in categorias):
            return 'categoria', valor
        if tipo == 'producto':
            coincidencia = next((p for p in productos if str(p.pk) == valor), None)
            if coincidencia:
                return 'producto', coincidencia
    if categorias:
        return 'categoria', categorias[0]['valor']
    if productos:
        return 'producto', productos[0]
    return None, None


def grafico_prediccion_demanda(origen, tipo, valor):
    """Serie diaria de demanda real (PedidoDetalle.cantidad_solicitada, la
    misma fuente que usa el motor de predicción) más el pronóstico del
    último lote de Prediccion, para el gráfico de predicción de demanda
    del dashboard — de una categoría completa (sumando todos sus
    productos) o de un producto suelto, según "tipo".

    El histórico y el pronóstico NUNCA se recortan al rango de fechas del
    dashboard (el filtro Hoy/7 días/30 días/Personalizado, u otro): son dos
    cosas distintas. El pronóstico siempre se agrega completo, aunque sus
    fechas caigan más allá de "hoy" — ese tramo futuro es justamente lo que
    hay que ver. Real y pronóstico nunca se mezclan en el mismo punto: se
    separan por 'fecha_corte' (el día justo antes de que empiece el
    pronóstico), que la plantilla dibuja pasando de línea continua a
    punteada."""
    if tipo is None:
        return None

    if tipo == 'producto':
        producto = valor
        productos_ids = [producto.pk]
        etiqueta = f'{producto.codigo} — {producto.nombre} {producto.presentacion}'.strip()
    else:
        qs = Producto.objects.filter(categoria=valor, predicciones__isnull=False)
        if origen:
            qs = qs.filter(origen=origen)
        productos_ids = list(qs.distinct().values_list('pk', flat=True))
        etiqueta = dict(Categoria.choices).get(valor, valor)
        if not productos_ids:
            return None

    ultimas_por_producto = _ultima_prediccion_por_producto(productos_ids)
    pronostico_por_fecha = {}
    if ultimas_por_producto:
        filas = Prediccion.objects.filter(producto_id__in=productos_ids).values(
            'producto_id', 'fecha_objetivo', 'fecha_generacion', 'demanda_predicha',
        )
        for fila in filas:
            if fila['fecha_generacion'] != ultimas_por_producto.get(fila['producto_id']):
                continue
            clave = fila['fecha_objetivo']
            pronostico_por_fecha[clave] = pronostico_por_fecha.get(clave, 0.0) + fila['demanda_predicha']
    pronostico = sorted(pronostico_por_fecha.items())
    primera_fecha_pronostico = pronostico[0][0] if pronostico else None

    # Ventana de histórico: proporcional a lo que dure el pronóstico (el
    # triple de días, entre 21 y 60), para que el tramo pronosticado se vea
    # con un tamaño razonable en vez de quedar como una astilla apretada
    # contra 60 días fijos de historia cuando el horizonte es de solo 7 días.
    # Sin pronóstico todavía, se ancla a la última fecha real disponible (no
    # a "hoy" del reloj del servidor: los datos de la tesis son de un
    # periodo fijo, y "hoy" podría no tener nada que ver con él).
    if primera_fecha_pronostico:
        fin_historico = primera_fecha_pronostico - timedelta(days=1)
        dias_pronostico = (pronostico[-1][0] - primera_fecha_pronostico).days + 1
        dias_historico = max(21, min(60, dias_pronostico * 3))
    else:
        detalles_qs = PedidoDetalle.objects.filter(producto_id__in=productos_ids)
        if origen:
            detalles_qs = detalles_qs.filter(pedido__origen=origen)
        ultima_fecha_real = detalles_qs.aggregate(m=Max('pedido__fecha_solicitud'))['m']
        fin_historico = ultima_fecha_real.date() if ultima_fecha_real else date.today()
        dias_historico = 30
    inicio_historico = fin_historico - timedelta(days=dias_historico - 1)

    detalles_qs = PedidoDetalle.objects.filter(
        producto_id__in=productos_ids,
        pedido__fecha_solicitud__date__gte=inicio_historico,
        pedido__fecha_solicitud__date__lte=fin_historico,
    )
    if origen:
        detalles_qs = detalles_qs.filter(pedido__origen=origen)
    reales_por_fecha = {
        fila['pedido__fecha_solicitud__date']: float(fila['total'])
        for fila in detalles_qs.values('pedido__fecha_solicitud__date').annotate(total=Sum('cantidad_solicitada'))
    }

    puntos = []
    dia = inicio_historico
    while dia <= fin_historico:
        puntos.append({'fecha': dia.isoformat(), 'real': reales_por_fecha.get(dia, 0.0), 'pronostico': None})
        dia += timedelta(days=1)
    for fecha_objetivo, total in pronostico:
        puntos.append({'fecha': fecha_objetivo.isoformat(), 'real': None, 'pronostico': total})

    return {
        'tipo': tipo,
        'etiqueta': etiqueta,
        'puntos_json': json.dumps(puntos),
        'hay_pronostico': bool(pronostico),
        'fecha_corte': fin_historico if pronostico else None,
    }


@login_required
def dashboard(request):
    origen = _origen_o_defecto(request)
    fecha_inicio, fecha_fin, rango = resolver_rango_periodo(request, origen)

    ei = calcular_ei(fecha_inicio, fecha_fin, origen=origen)
    ns = calcular_ns(fecha_inicio, fecha_fin, origen=origen)
    coi = calcular_coi(fecha_inicio, fecha_fin, origen=origen)

    # Periodo anterior: misma duración, inmediatamente antes del rango
    # mostrado, para poder decir "subió/bajó X% contra el periodo anterior"
    # en las tarjetas de indicadores.
    duracion_dias = (fecha_fin - fecha_inicio).days + 1
    fecha_fin_anterior = fecha_inicio - timedelta(days=1)
    fecha_inicio_anterior = fecha_fin_anterior - timedelta(days=duracion_dias - 1)
    ei_anterior = calcular_ei(fecha_inicio_anterior, fecha_fin_anterior, origen=origen)
    ns_anterior = calcular_ns(fecha_inicio_anterior, fecha_fin_anterior, origen=origen)
    coi_anterior = calcular_coi(fecha_inicio_anterior, fecha_fin_anterior, origen=origen)

    # El dashboard es para verlo de un vistazo, no para scrollear un
    # listado completo: muestra solo los primeros casos (ya vienen
    # ordenados por urgencia/severidad) y enlaza a la pantalla con el
    # listado paginado completo para el resto.
    LIMITE_PREVIA = 8
    recomendaciones_todas = _recomendaciones_urgentes(origen)
    anomalias_todas = _anomalias_no_revisadas(origen)

    # Recomendación destacada del dashboard: la más urgente que todavía no
    # tiene una decisión tomada (aceptada is None). El resto sigue
    # disponible completo en /recomendaciones.
    recomendacion_destacada = next((r for r in recomendaciones_todas if r.aceptada is None), None)

    categorias_prediccion = _categorias_con_prediccion(origen)
    productos_prediccion = _productos_con_prediccion(origen)
    tipo_grafico, valor_grafico = _elegir_seleccion_grafico(
        categorias_prediccion, productos_prediccion, request.GET.get('serie_grafico'),
    )

    contexto = {
        'fecha_inicio': fecha_inicio,
        'fecha_fin': fecha_fin,
        'origen': origen or '',
        'rango_activo': rango,
        'ei': ei,
        'ns': ns,
        'coi': coi,
        'comparacion_ei': _variacion_periodo(ei['valor'], ei_anterior['valor'], mejor_si_sube=True),
        'comparacion_ns': _variacion_periodo(ns['valor'], ns_anterior['valor'], mejor_si_sube=True),
        'comparacion_coi': _variacion_periodo(coi['valor'], coi_anterior['valor'], mejor_si_sube=False),
        'recomendaciones': recomendaciones_todas[:LIMITE_PREVIA],
        'recomendaciones_total': len(recomendaciones_todas),
        'recomendacion_destacada': recomendacion_destacada,
        'anomalias': anomalias_todas[:LIMITE_PREVIA],
        'anomalias_total': len(anomalias_todas),
        'anomalias_severidad': _anomalias_por_severidad(origen),
        'salud_catalogo': _salud_catalogo(origen),
        'categorias_prediccion': categorias_prediccion,
        'productos_prediccion': productos_prediccion,
        'tipo_grafico': tipo_grafico,
        'categoria_grafico': valor_grafico if tipo_grafico == 'categoria' else None,
        'producto_grafico_pk': valor_grafico.pk if tipo_grafico == 'producto' else None,
        'grafico_prediccion': grafico_prediccion_demanda(origen, tipo_grafico, valor_grafico),
    }
    # El formulario de filtros pide esta misma URL por HTMX y solo necesita
    # la zona de resultados: la plantilla completa (con sidebar, etc.) sería
    # trabajo de red y de parseo desperdiciado en cada filtro.
    plantilla = (
        'inventario/_dashboard_resultados.html' if request.headers.get('HX-Request') == 'true'
        else 'inventario/dashboard.html'
    )
    return render(request, plantilla, contexto)


@login_required
def dashboard_grafico_prediccion(request):
    """Solo la tarjeta del gráfico de predicción de demanda: el selector de
    categoría/producto la vuelve a pedir por htmx sin recargar el resto del
    dashboard (indicadores, tablas, etc.), que no dependen de la serie
    elegida aquí."""
    origen = _origen_o_defecto(request)
    categorias_prediccion = _categorias_con_prediccion(origen)
    productos_prediccion = _productos_con_prediccion(origen)
    tipo_grafico, valor_grafico = _elegir_seleccion_grafico(
        categorias_prediccion, productos_prediccion, request.GET.get('serie_grafico'),
    )
    contexto = {
        'origen': origen or '',
        'categorias_prediccion': categorias_prediccion,
        'productos_prediccion': productos_prediccion,
        'tipo_grafico': tipo_grafico,
        'categoria_grafico': valor_grafico if tipo_grafico == 'categoria' else None,
        'producto_grafico_pk': valor_grafico.pk if tipo_grafico == 'producto' else None,
        'grafico_prediccion': grafico_prediccion_demanda(origen, tipo_grafico, valor_grafico),
    }
    return render(request, 'inventario/_dashboard_grafico_prediccion.html', contexto)
