"""Pantallas de registro y listado de pedidos."""
from datetime import datetime, time

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import F
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..forms import CompletarLineaPedidoForm, LineaPedidoFormSet, PedidoForm, etiqueta_producto, evaluar_linea_pedido
from ..models import Origen, Pedido, PedidoDetalle, Producto
from ._comunes import _parsear_fecha, _parsear_origen


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
    plantilla = (
        'inventario/_pedido_lista_resultados.html' if request.headers.get('HX-Request') == 'true'
        else 'inventario/pedido_lista.html'
    )
    return render(request, plantilla, contexto)


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


@login_required
def pedido_validar_linea(request):
    """Fragmento HTMX: valida en vivo, línea por línea, la relación entre
    cantidad solicitada y atendida (misma regla que evaluar_linea_pedido,
    usada por LineaPedidoForm y CompletarLineaPedidoForm), antes de enviar
    el formulario de pedido o de completar una línea desde el listado."""
    try:
        solicitada = int(request.GET.get('cantidad_solicitada', ''))
    except ValueError:
        return render(request, 'inventario/_aviso_validacion.html', {'aviso': None})

    atendida_texto = request.GET.get('cantidad_atendida', '').strip()
    atendida = int(atendida_texto) if atendida_texto.isdigit() else 0
    motivo = request.GET.get('motivo_no_atencion', '')

    error = evaluar_linea_pedido(solicitada, atendida, motivo)
    aviso = error[1] if error else None
    return render(request, 'inventario/_aviso_validacion.html', {'aviso': aviso})
