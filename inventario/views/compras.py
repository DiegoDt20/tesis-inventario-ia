"""Compras a proveedores: listado, registro (a mano o desde las
recomendaciones aceptadas), recepción de mercadería y cierre.

Recibir una línea genera el Movimiento de ingreso (CompraDetalle.save()),
que es lo que sube el stock; aquí nunca se escribe stock_actual. Lo pedido
y aún no recibido de compras no canceladas es lo que el motor de
decisiones descuenta como "pedidos en tránsito"."""
from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, F, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from ..forms import CompraForm, LineaCompraFormSet, etiqueta_producto
from ..models import Compra, CompraDetalle, Origen, Producto, Proveedor, Recomendacion
from ._comunes import _parsear_fecha


def recomendaciones_por_pedir():
    """Recomendaciones aceptadas del último lote que sugieren comprar algo
    y todavía no tienen una línea en una compra vigente (no cancelada)."""
    ultima = Recomendacion.objects.order_by('-fecha_generacion').values_list('fecha_generacion', flat=True).first()
    if ultima is None:
        return []
    candidatas = (
        Recomendacion.objects.filter(fecha_generacion=ultima, aceptada=True)
        .select_related('producto').prefetch_related('compra_detalles__compra')
        .order_by('producto__nombre')
    )
    return [r for r in candidatas if r.unidades_sugeridas > 0 and r.linea_compra is None]


def _etiquetas_producto():
    return [etiqueta_producto(p) for p in Producto.objects.filter(activo=True).order_by('nombre')]


@login_required
@permission_required('inventario.view_compra', raise_exception=True)
def compra_lista(request):
    estado = request.GET.get('estado') or ''
    qs = Compra.objects.select_related('proveedor').annotate(
        lineas=Count('detalles'),
        unidades_pedidas=Sum('detalles__cantidad_pedida'),
        unidades_recibidas=Sum('detalles__cantidad_recibida'),
        monto=Sum(F('detalles__cantidad_pedida') * F('detalles__costo_unitario')),
    ).order_by('-fecha_pedido', '-pk')
    if estado in Compra.Estado.values:
        qs = qs.filter(estado=estado)

    parametros = request.GET.copy()
    parametros.pop('page', None)
    return render(request, 'inventario/compra_lista.html', {
        'pagina': Paginator(qs, 25).get_page(request.GET.get('page')),
        'estado': estado,
        'estados': Compra.Estado.choices,
        'querystring': parametros.urlencode(),
        'por_pedir': len(recomendaciones_por_pedir()),
    })


@login_required
@permission_required('inventario.add_compra', raise_exception=True)
def compra_nueva(request):
    desde_recomendaciones = request.GET.get('desde') == 'recomendaciones'
    if request.method == 'POST':
        form = CompraForm(request.POST)
        formset = LineaCompraFormSet(request.POST)
        if 'agregar_linea' in request.POST:
            # Solo se agrega una línea vacía y se redibuja, sin validar.
            datos = request.POST.copy()
            datos['form-TOTAL_FORMS'] = str(int(datos.get('form-TOTAL_FORMS', 1)) + 1)
            formset = LineaCompraFormSet(datos)
        elif form.is_valid() and formset.is_valid():
            lineas = [f.cleaned_data for f in formset if f.cleaned_data and not f.cleaned_data.get('DELETE')]
            if not lineas:
                messages.error(request, 'Agrega al menos una línea a la compra.')
            else:
                # Solo se enlaza una recomendación aceptada del mismo producto
                # (el id viaja en un campo oculto del formulario).
                recomendaciones = {
                    r.pk: r for r in Recomendacion.objects.filter(
                        pk__in=[l['recomendacion'] for l in lineas if l.get('recomendacion')], aceptada=True,
                    )
                }
                with transaction.atomic():
                    compra = form.save()
                    for linea in lineas:
                        recomendacion = recomendaciones.get(linea.get('recomendacion'))
                        if recomendacion and recomendacion.producto_id != linea['producto'].pk:
                            recomendacion = None
                        CompraDetalle.objects.create(
                            compra=compra, producto=linea['producto'],
                            cantidad_pedida=linea['cantidad_pedida'],
                            costo_unitario=linea['costo_unitario'],
                            recomendacion=recomendacion,
                        )
                messages.success(request, f'Compra #{compra.pk} registrada con {len(lineas)} línea(s).')
                return redirect('inventario:compra_detalle', pk=compra.pk)
    else:
        form = CompraForm()
        iniciales = []
        if desde_recomendaciones:
            iniciales = [
                {
                    'producto': etiqueta_producto(r.producto),
                    'cantidad_pedida': r.unidades_sugeridas,
                    'costo_unitario': r.producto.costo_compra,
                    'recomendacion': r.pk,
                }
                for r in recomendaciones_por_pedir()
            ]
            if not iniciales:
                messages.info(request, 'No hay recomendaciones aceptadas pendientes de pedir.')
        formset = LineaCompraFormSet(initial=iniciales)

    return render(request, 'inventario/compra_form.html', {
        'form': form,
        'formset': formset,
        'etiquetas_producto': _etiquetas_producto(),
        'hay_proveedores': Proveedor.objects.exists(),
        'desde_recomendaciones': desde_recomendaciones,
    })


def _lineas(compra):
    lineas = list(compra.detalles.select_related('producto', 'recomendacion').order_by('pk'))
    for linea in lineas:
        linea.pendiente = max(linea.cantidad_pedida - linea.cantidad_recibida, 0)
        linea.subtotal = linea.cantidad_pedida * linea.costo_unitario
    return lineas


@login_required
@permission_required('inventario.view_compra', raise_exception=True)
def compra_detalle(request, pk):
    compra = get_object_or_404(Compra.objects.select_related('proveedor'), pk=pk)
    lineas = _lineas(compra)
    return render(request, 'inventario/compra_detalle.html', {
        'compra': compra,
        'lineas': lineas,
        'total': sum(l.subtotal for l in lineas),
        'unidades_pendientes': sum(l.pendiente for l in lineas),
        'unidades_recibidas': sum(l.cantidad_recibida for l in lineas),
        'hoy': date.today(),
    })


@login_required
@permission_required('inventario.change_compra', raise_exception=True)
@require_POST
def compra_recibir(request, pk):
    """Registra lo que llegó de una compra pendiente: por cada línea, las
    unidades que llegan ahora (0 = no llegó nada de esa línea). Cada línea
    recibida genera su ingreso de stock con origen real. Cuando no queda
    nada pendiente, la compra pasa a "recibida"."""
    compra = get_object_or_404(Compra, pk=pk)
    destino = redirect('inventario:compra_detalle', pk=compra.pk)
    if compra.estado != Compra.Estado.PENDIENTE:
        messages.error(request, 'Solo se puede recibir mercadería de una compra pendiente.')
        return destino

    fecha = _parsear_fecha(request.POST.get('fecha_llegada')) or date.today()
    if fecha < compra.fecha_pedido:
        messages.error(request, 'La fecha de llegada no puede ser anterior a la fecha del pedido.')
        return destino
    if fecha > date.today():
        messages.error(request, 'La fecha de llegada no puede ser futura.')
        return destino

    lineas = _lineas(compra)
    llegadas = []
    for linea in lineas:
        texto = (request.POST.get(f'recibir_{linea.pk}') or '0').strip()
        try:
            cantidad = int(texto)
        except ValueError:
            cantidad = -1
        if cantidad < 0 or cantidad > linea.pendiente:
            messages.error(
                request,
                f'{linea.producto.codigo}: la cantidad recibida debe estar entre 0 y {linea.pendiente} '
                '(lo que falta por llegar). No se registró nada.',
            )
            return destino
        if cantidad:
            llegadas.append((linea, cantidad))

    if not llegadas:
        messages.error(request, 'Indica cuántas unidades llegaron en al menos una línea.')
        return destino

    with transaction.atomic():
        for linea, cantidad in llegadas:
            linea.cantidad_recibida += cantidad
            linea.fecha_llegada_linea = fecha
            linea.save(usuario=request.user, origen=Origen.REAL)
        completa = all(l.cantidad_recibida >= l.cantidad_pedida for l in lineas)
        if completa:
            compra.estado = Compra.Estado.RECIBIDA
            compra.fecha_llegada = fecha
            compra.save(update_fields=['estado', 'fecha_llegada'])

    unidades = sum(cantidad for _, cantidad in llegadas)
    messages.success(
        request,
        f'Recibidas {unidades} unidad(es) en {len(llegadas)} línea(s); el stock ya se actualizó. '
        + ('La compra quedó completa.' if completa else 'La compra sigue pendiente por lo que falta.'),
    )
    return destino


@login_required
@permission_required('inventario.change_compra', raise_exception=True)
@require_POST
def compra_cerrar(request, pk):
    """Cierra una compra pendiente cuyo resto ya no va a llegar. Si no se
    recibió nada, queda "cancelada". Si se recibió una parte, queda
    "recibida" con lo que llegó: la cantidad pedida de cada línea se baja a
    la recibida, para que lo que falta deje de contarse como pedido en
    tránsito (el motor descuenta pedida − recibida de toda compra no
    cancelada). Las líneas de las que no llegó nada se eliminan."""
    compra = get_object_or_404(Compra, pk=pk)
    destino = redirect('inventario:compra_detalle', pk=compra.pk)
    if compra.estado != Compra.Estado.PENDIENTE:
        messages.error(request, 'Solo se puede cerrar una compra pendiente.')
        return destino

    lineas = list(compra.detalles.all())
    recibidas = [l for l in lineas if l.cantidad_recibida > 0]
    with transaction.atomic():
        if not recibidas:
            compra.estado = Compra.Estado.CANCELADA
            compra.save(update_fields=['estado'])
            messages.success(request, f'Compra #{compra.pk} cancelada: no se recibió nada.')
            return destino
        for linea in lineas:
            if linea.cantidad_recibida == 0:
                linea.delete()
            elif linea.cantidad_pedida != linea.cantidad_recibida:
                linea.cantidad_pedida = linea.cantidad_recibida
                linea.save(update_fields=['cantidad_pedida'])
        compra.estado = Compra.Estado.RECIBIDA
        compra.fecha_llegada = max(l.fecha_llegada_linea or compra.fecha_pedido for l in recibidas)
        compra.save(update_fields=['estado', 'fecha_llegada'])
    messages.success(request, f'Compra #{compra.pk} cerrada con lo recibido; lo que faltaba ya no se espera.')
    return destino
