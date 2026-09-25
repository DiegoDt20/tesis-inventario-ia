"""Pantallas de registro y listado de movimientos de stock (ingresos,
salidas, ajustes)."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.shortcuts import redirect, render
from django.utils import timezone

from ..forms import MovimientoForm, etiqueta_producto, resolver_producto
from ..models import Movimiento, Origen, Producto
from ._comunes import _parsear_fecha


def _querystring_sin_pagina(request):
    """Los filtros activos (tipo, producto, fechas) como querystring, sin
    "page", para que los enlaces de paginación no los pierdan."""
    parametros = request.GET.copy()
    parametros.pop('page', None)
    return parametros.urlencode()


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

    hoy = timezone.localdate()
    contexto = {
        'form': form,
        'etiquetas_producto': [etiqueta_producto(p) for p in Producto.objects.filter(activo=True).order_by('nombre')],
        # Panel lateral: para confirmar de un vistazo lo que ya se registró
        # hoy, sin tener que ir al listado completo.
        'movimientos_hoy': Movimiento.objects.select_related('producto').filter(fecha__date=hoy).order_by('-fecha')[:15],
    }
    return render(request, 'inventario/movimiento_form.html', contexto)


@login_required
def movimiento_validar(request):
    """Fragmento HTMX: avisa en vivo si una salida dejaría el stock del
    producto elegido en negativo, antes de que el operador intente guardar
    (Movimiento.clean() ya lo impide al guardar; esto solo adelanta el
    aviso). Reutiliza resolver_producto, el mismo buscador que el resto del
    formulario, para no duplicar esa lógica."""
    producto = resolver_producto(request.GET.get('producto', ''))
    tipo = request.GET.get('tipo')
    aviso = None
    if producto and tipo == Movimiento.Tipo.SALIDA:
        try:
            cantidad = int(request.GET.get('cantidad', ''))
        except ValueError:
            cantidad = None
        if cantidad is not None and cantidad > producto.stock_actual:
            aviso = (
                f'Esta salida dejaría el stock de {producto.codigo} en negativo: '
                f'solo hay {producto.stock_actual} unidad(es) disponibles.'
            )
    return render(request, 'inventario/_aviso_validacion.html', {'aviso': aviso})


@login_required
def movimiento_contexto_producto(request):
    """Fragmento HTMX: al elegir un producto en el formulario, muestra su
    stock actual, stock mínimo, categoría y sus últimos tres movimientos —
    lo que el operador necesita para decidir sin cambiar de pantalla."""
    producto = resolver_producto(request.GET.get('producto', ''))
    ultimos_movimientos = []
    if producto is not None:
        ultimos_movimientos = list(producto.movimientos.order_by('-fecha')[:3])
    return render(
        request, 'inventario/_contexto_producto.html',
        {'producto': producto, 'ultimos_movimientos': ultimos_movimientos},
    )


@login_required
def movimiento_lista(request):
    tipo = request.GET.get('tipo') or ''
    producto = resolver_producto(request.GET.get('producto', ''))
    fecha_inicio = _parsear_fecha(request.GET.get('fecha_inicio'))
    fecha_fin = _parsear_fecha(request.GET.get('fecha_fin'))

    qs = Movimiento.objects.select_related('producto', 'usuario').prefetch_related('anomalias_detectadas')
    if tipo in Movimiento.Tipo.values:
        qs = qs.filter(tipo=tipo)
    if producto:
        qs = qs.filter(producto=producto)
    if fecha_inicio:
        qs = qs.filter(fecha__date__gte=fecha_inicio)
    if fecha_fin:
        qs = qs.filter(fecha__date__lte=fecha_fin)

    pagina = Paginator(qs, 25).get_page(request.GET.get('page'))

    contexto = {
        'pagina': pagina,
        'tipo': tipo,
        'producto_texto': etiqueta_producto(producto) if producto else request.GET.get('producto', ''),
        'fecha_inicio': fecha_inicio,
        'fecha_fin': fecha_fin,
        'tipos': Movimiento.Tipo.choices,
        'etiquetas_producto': [etiqueta_producto(p) for p in Producto.objects.filter(activo=True).order_by('nombre')],
        'querystring': _querystring_sin_pagina(request),
    }
    plantilla = (
        'inventario/_movimiento_lista_resultados.html' if request.headers.get('HX-Request') == 'true'
        else 'inventario/movimiento_lista.html'
    )
    return render(request, plantilla, contexto)
