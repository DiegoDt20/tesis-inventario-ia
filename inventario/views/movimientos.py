"""Pantalla de registro de movimientos de stock (ingresos, salidas, ajustes)."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.shortcuts import redirect, render
from django.utils import timezone

from ..forms import MovimientoForm, etiqueta_producto, resolver_producto
from ..models import Movimiento, Origen, Producto


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
