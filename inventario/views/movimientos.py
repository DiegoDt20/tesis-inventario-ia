"""Pantalla de registro de movimientos de stock (ingresos, salidas, ajustes)."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.shortcuts import redirect, render
from django.utils import timezone

from ..forms import MovimientoForm, etiqueta_producto
from ..models import Origen, Producto


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
