"""Vistas del sistema.

El dashboard es la pantalla de indicadores (para la tesis). El resto son
las pantallas de operación diaria: pensadas para que el dueño de la
microempresa, sin conocimientos técnicos, registre pedidos, movimientos y
conteos, y decida sobre las recomendaciones y anomalías detectadas.
"""
import json
from datetime import date, datetime, time, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Case, Count, F, IntegerField, Max, Min, Sum, When
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import CompletarLineaPedidoForm, LineaPedidoFormSet, MovimientoForm, PedidoForm, etiqueta_producto
from .indicadores import (
    calcular_coi,
    calcular_ei,
    calcular_ns,
    hay_mezcla_de_origenes,
    rango_disponible,
    serie_mensual,
)
from .models import (
    Anomalia,
    Categoria,
    ConteoDetalle,
    ConteoFisico,
    ModeloEntrenado,
    Movimiento,
    Origen,
    Pedido,
    PedidoDetalle,
    Producto,
    Recomendacion,
)


def _parsear_fecha(texto):
    if not texto:
        return None
    try:
        return datetime.strptime(texto, '%Y-%m-%d').date()
    except ValueError:
        return None


def _parsear_origen(texto):
    """El selector de origen del dashboard: '' = todos, o un valor válido
    de Origen. Cualquier otra cosa se trata como "todos" (sin filtrar)."""
    if texto in (Origen.PRUEBA, Origen.REAL):
        return texto
    return None


def _querystring_sin_pagina(request):
    """Los filtros activos (fecha, estado, origen) como querystring, sin
    "page", para que los enlaces de paginación no los pierdan."""
    parametros = request.GET.copy()
    parametros.pop('page', None)
    return parametros.urlencode()


def _atendido_a_tiempo(fecha_atencion, fecha_requerida):
    """atendido_a_tiempo no se pregunta en ninguna pantalla: sale de
    comparar la fecha en que se atendió con la fecha que el cliente pidió.
    Sin una fecha límite conocida, se asume a tiempo (no hay con qué
    incumplir)."""
    if fecha_atencion is None:
        return False
    if fecha_requerida is None:
        return True
    fecha = fecha_atencion.date() if hasattr(fecha_atencion, 'date') else fecha_atencion
    return fecha <= fecha_requerida


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


def _anomalias_no_revisadas(origen):
    """Anomalías (diferencias de inventario y movimientos atípicos) que
    todavía nadie marcó como revisadas, ordenadas por severidad (alta
    primero) y luego por fecha de detección más reciente."""
    orden_severidad = Case(
        When(severidad=Anomalia.Severidad.ALTA, then=0),
        When(severidad=Anomalia.Severidad.MEDIA, then=1),
        default=2,
        output_field=IntegerField(),
    )
    qs = Anomalia.objects.filter(revisada=False)
    if origen:
        qs = qs.filter(producto__origen=origen)
    return list(
        qs.select_related('producto').annotate(orden_severidad=orden_severidad)
        .order_by('orden_severidad', '-fecha_deteccion')
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
    return render(request, 'inventario/dashboard.html', contexto)


# ----------------------------------------------------------------------
# Pedidos
# ----------------------------------------------------------------------

@login_required
def pedido_nuevo(request):
    if request.method == 'POST':
        pedido_form = PedidoForm(request.POST)
        formset = LineaPedidoFormSet(request.POST)

        if 'agregar_linea' in request.POST:
            # Solo se agrega una línea vacía más y se redibuja: no se valida
            # nada todavía, para no mostrarle errores a alguien que solo
            # quiere seguir llenando el pedido.
            datos = request.POST.copy()
            total_actual = int(datos.get('form-TOTAL_FORMS', 1))
            datos['form-TOTAL_FORMS'] = str(total_actual + 1)
            formset = LineaPedidoFormSet(datos)
        elif pedido_form.is_valid() and formset.is_valid():
            lineas = [f.cleaned_data for f in formset if f.cleaned_data]
            if not lineas:
                messages.error(request, 'Agrega al menos una línea al pedido.')
            else:
                fecha_solicitud = timezone.make_aware(
                    datetime.combine(pedido_form.cleaned_data['fecha_solicitud'], time.min)
                )
                todas_completas = all(
                    linea['cantidad_atendida'] >= linea['cantidad_solicitada'] for linea in lineas
                )
                with transaction.atomic():
                    pedido = Pedido.objects.create(
                        fecha_solicitud=fecha_solicitud,
                        cliente=pedido_form.cleaned_data['cliente'],
                        canal=pedido_form.cleaned_data['canal'],
                        estado=Pedido.Estado.ATENDIDO if todas_completas else Pedido.Estado.PENDIENTE,
                        origen=Origen.REAL,
                    )
                    for linea in lineas:
                        solicitada = linea['cantidad_solicitada']
                        atendida = linea['cantidad_atendida']
                        fecha_requerida = linea.get('fecha_requerida')
                        fecha_atencion = fecha_solicitud if atendida > 0 else None
                        completa = atendida >= solicitada
                        PedidoDetalle.objects.create(
                            pedido=pedido,
                            producto=linea['producto'],
                            cantidad_solicitada=solicitada,
                            cantidad_atendida=atendida,
                            fecha_requerida=fecha_requerida,
                            fecha_atencion=fecha_atencion,
                            atendido_a_tiempo=(
                                _atendido_a_tiempo(fecha_atencion, fecha_requerida) if completa else False
                            ),
                            motivo_no_atencion=None if completa else (linea.get('motivo_no_atencion') or None),
                        )
                messages.success(request, f'Pedido #{pedido.pk} registrado con {len(lineas)} línea(s).')
                return redirect('inventario:pedido_lista')
    else:
        pedido_form = PedidoForm()
        formset = LineaPedidoFormSet()

    contexto = {
        'pedido_form': pedido_form,
        'formset': formset,
        'etiquetas_producto': [etiqueta_producto(p) for p in Producto.objects.filter(activo=True).order_by('nombre')],
    }
    return render(request, 'inventario/pedido_form.html', contexto)


@login_required
def pedido_lista(request):
    """Una fila por línea de pedido (no por pedido): así se puede comparar
    de un vistazo, ordenar y paginar sin que un pedido con 10 productos
    ocupe 10 veces más espacio que uno con 1."""
    origen = _parsear_origen(request.GET.get('origen'))
    fecha = _parsear_fecha(request.GET.get('fecha'))
    estado = request.GET.get('estado') or ''

    qs = PedidoDetalle.objects.select_related('pedido', 'producto').order_by(
        '-pedido__fecha_solicitud', 'pedido_id', 'id',
    )
    if origen:
        qs = qs.filter(pedido__origen=origen)
    if fecha:
        qs = qs.filter(pedido__fecha_solicitud__date=fecha)
    if estado in Pedido.Estado.values:
        qs = qs.filter(pedido__estado=estado)

    pagina = Paginator(qs, 25).get_page(request.GET.get('page'))

    # Se cuelga el formulario de completar directamente del objeto detalle
    # (en vez de un dict aparte) porque las plantillas Django no pueden
    # indexar un dict con una clave variable sin un filtro a medida.
    for detalle in pagina.object_list:
        detalle.formulario_completar = None
        if detalle.cantidad_atendida < detalle.cantidad_solicitada:
            detalle.formulario_completar = CompletarLineaPedidoForm(
                cantidad_solicitada=detalle.cantidad_solicitada,
                initial={'cantidad_atendida': detalle.cantidad_atendida},
                prefix=f'linea{detalle.pk}',
            )

    contexto = {
        'pagina': pagina,
        'fecha': fecha,
        'estado': estado,
        'origen': origen or '',
        'estados': Pedido.Estado.choices,
        'querystring': _querystring_sin_pagina(request),
    }
    return render(request, 'inventario/pedido_lista.html', contexto)


@login_required
@require_POST
def pedido_completar_linea(request, detalle_id):
    """Completa (o corrige) la atención de una línea de pedido desde el
    listado: cuánto se entregó y cuándo. atendido_a_tiempo se recalcula
    solo; si con esto el pedido completo queda atendido, también se
    actualiza su estado."""
    detalle = get_object_or_404(PedidoDetalle, pk=detalle_id)
    form = CompletarLineaPedidoForm(
        request.POST, cantidad_solicitada=detalle.cantidad_solicitada, prefix=f'linea{detalle.pk}',
    )

    if form.is_valid():
        atendida = form.cleaned_data['cantidad_atendida']
        fecha_atencion_dt = timezone.make_aware(
            datetime.combine(form.cleaned_data['fecha_atencion'], time.min)
        )
        completa = atendida >= detalle.cantidad_solicitada

        detalle.cantidad_atendida = atendida
        detalle.fecha_atencion = fecha_atencion_dt
        detalle.atendido_a_tiempo = (
            _atendido_a_tiempo(fecha_atencion_dt, detalle.fecha_requerida) if completa else False
        )
        detalle.motivo_no_atencion = None if completa else (form.cleaned_data.get('motivo_no_atencion') or None)
        detalle.save(update_fields=[
            'cantidad_atendida', 'fecha_atencion', 'atendido_a_tiempo', 'motivo_no_atencion',
        ])

        pedido = detalle.pedido
        if not pedido.detalles.filter(cantidad_atendida__lt=F('cantidad_solicitada')).exists():
            pedido.estado = Pedido.Estado.ATENDIDO
            pedido.save(update_fields=['estado'])

        messages.success(request, f'Línea de {detalle.producto.codigo} actualizada.')
    else:
        errores = ' '.join(f'{campo}: {", ".join(msgs)}' for campo, msgs in form.errors.items())
        messages.error(request, f'No se pudo actualizar la línea de {detalle.producto.codigo}. {errores}')

    return redirect(request.META.get('HTTP_REFERER') or reverse('inventario:pedido_lista'))


# ----------------------------------------------------------------------
# Movimientos
# ----------------------------------------------------------------------

@login_required
def movimiento_nuevo(request):
    if request.method == 'POST':
        form = MovimientoForm(request.POST)
        if form.is_valid():
            movimiento = form.save(commit=False)
            movimiento.usuario = request.user
            movimiento.fecha = timezone.now()
            movimiento.origen = Origen.REAL
            try:
                movimiento.save()
            except ValidationError as error:
                # Defensa adicional a la de Movimiento.clean() (que ya
                # corrió en form.is_valid()): por si el stock cambió justo
                # entre la validación y el guardado.
                form.add_error(None, error)
            else:
                messages.success(
                    request,
                    f'Movimiento registrado: {movimiento.get_tipo_display()} de '
                    f'{movimiento.cantidad} unidades de {movimiento.producto.codigo}.',
                )
                return redirect('inventario:movimiento_nuevo')
    else:
        form = MovimientoForm()

    contexto = {
        'form': form,
        'etiquetas_producto': [etiqueta_producto(p) for p in Producto.objects.filter(activo=True).order_by('nombre')],
    }
    return render(request, 'inventario/movimiento_form.html', contexto)


# ----------------------------------------------------------------------
# Conteo físico
# ----------------------------------------------------------------------

@login_required
def conteo_nuevo(request):
    fecha_corte = _parsear_fecha(request.GET.get('fecha')) or date.today()
    conteo = ConteoFisico.objects.filter(fecha_corte=fecha_corte, origen=Origen.REAL).order_by('-pk').first()

    if request.method == 'POST':
        fecha_corte = _parsear_fecha(request.POST.get('fecha_corte')) or fecha_corte
        responsable = request.POST.get('responsable', '').strip() or request.user.get_username()
        conteo = ConteoFisico.objects.filter(fecha_corte=fecha_corte, origen=Origen.REAL).order_by('-pk').first()

        guardados = 0
        errores = []
        with transaction.atomic():
            for producto in Producto.objects.filter(activo=True):
                valor_texto = request.POST.get(f'stock_{producto.pk}', '').strip()
                if not valor_texto:
                    continue
                try:
                    stock_fisico = int(valor_texto)
                    if stock_fisico < 0:
                        raise ValueError
                except ValueError:
                    errores.append(f'{producto.codigo}: "{valor_texto}" no es una cantidad válida.')
                    continue

                if conteo is None:
                    conteo = ConteoFisico.objects.create(
                        fecha_corte=fecha_corte, responsable=responsable, origen=Origen.REAL,
                    )

                ConteoDetalle.objects.update_or_create(
                    conteo=conteo, producto=producto,
                    defaults={'stock_sistema': producto.stock_actual, 'stock_fisico': stock_fisico},
                )
                guardados += 1

        if guardados:
            messages.success(
                request, f'Se guardaron {guardados} producto(s) del conteo del {fecha_corte:%d/%m/%Y}.',
            )
        if errores:
            messages.error(request, 'Algunos valores no se guardaron: ' + '; '.join(errores))
        if not guardados and not errores:
            messages.info(request, 'No se ingresó ningún valor nuevo.')

        return redirect(f"{reverse('inventario:conteo_nuevo')}?fecha={fecha_corte:%Y-%m-%d}")

    detalles_previos = {d.producto_id: d for d in conteo.detalles.all()} if conteo else {}
    filas = []
    for producto in Producto.objects.filter(activo=True).order_by('nombre'):
        detalle = detalles_previos.get(producto.pk)
        filas.append({
            'producto': producto,
            'stock_fisico_previo': detalle.stock_fisico if detalle else '',
            'diferencia_previa': detalle.diferencia if detalle else None,
        })

    contexto = {
        'fecha_corte': fecha_corte,
        'responsable': conteo.responsable if conteo else '',
        'conteo': conteo,
        'filas': filas,
        'total_productos': len(filas),
        'total_contados': len(detalles_previos),
    }
    return render(request, 'inventario/conteo_form.html', contexto)


# ----------------------------------------------------------------------
# Recomendaciones
# ----------------------------------------------------------------------

@login_required
@permission_required('inventario.change_recomendacion', raise_exception=True)
def recomendaciones_lista(request):
    ultima_fecha_generacion = (
        Recomendacion.objects.order_by('-fecha_generacion').values_list('fecha_generacion', flat=True).first()
    )
    pagina = None
    if ultima_fecha_generacion is not None:
        orden_urgencia = Case(
            When(estado=Recomendacion.Estado.CRITICO, then=0),
            When(estado=Recomendacion.Estado.REPONER, then=1),
            When(estado=Recomendacion.Estado.NORMAL, then=2),
            default=3,
            output_field=IntegerField(),
        )
        qs = (
            Recomendacion.objects.filter(fecha_generacion=ultima_fecha_generacion)
            .select_related('producto').annotate(orden_urgencia=orden_urgencia)
            .order_by('orden_urgencia', '-cantidad_sugerida')
        )
        pagina = Paginator(qs, 25).get_page(request.GET.get('page'))

    contexto = {'pagina': pagina, 'fecha_generacion': ultima_fecha_generacion}
    return render(request, 'inventario/recomendaciones_lista.html', contexto)


@login_required
@permission_required('inventario.change_recomendacion', raise_exception=True)
@require_POST
def recomendacion_decidir(request, pk):
    recomendacion = get_object_or_404(Recomendacion, pk=pk)
    accion = request.POST.get('accion')
    if accion in ('aceptar', 'rechazar'):
        recomendacion.aceptada = accion == 'aceptar'
        recomendacion.fecha_decision = timezone.now()
        recomendacion.save(update_fields=['aceptada', 'fecha_decision'])
        messages.success(
            request,
            f'Recomendación de {recomendacion.producto.codigo} marcada como '
            f'{"aceptada" if recomendacion.aceptada else "rechazada"}.',
        )
    return redirect('inventario:recomendaciones_lista')


# ----------------------------------------------------------------------
# Anomalías
# ----------------------------------------------------------------------

@login_required
@permission_required('inventario.change_anomalia', raise_exception=True)
def anomalias_lista(request):
    pagina = Paginator(_anomalias_no_revisadas(None), 25).get_page(request.GET.get('page'))
    return render(request, 'inventario/anomalias_lista.html', {'pagina': pagina})


@login_required
@permission_required('inventario.change_anomalia', raise_exception=True)
@require_POST
def anomalia_marcar_revisada(request, pk):
    anomalia = get_object_or_404(Anomalia, pk=pk)
    anomalia.revisada = True
    anomalia.fecha_revision = timezone.now()
    anomalia.save(update_fields=['revisada', 'fecha_revision'])
    messages.success(request, 'Anomalía marcada como revisada.')
    return redirect('inventario:anomalias_lista')
