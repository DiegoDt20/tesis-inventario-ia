"""Proveedores: listado, alta y edición."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render

from ..forms import ProveedorForm
from ..models import Compra, Proveedor


@login_required
@permission_required('inventario.view_proveedor', raise_exception=True)
def proveedor_lista(request):
    proveedores = Proveedor.objects.annotate(
        total_compras=Count('compras'),
        compras_pendientes=Count('compras', filter=Q(compras__estado=Compra.Estado.PENDIENTE)),
    )
    return render(request, 'inventario/proveedor_lista.html', {'proveedores': proveedores})


@login_required
@permission_required('inventario.add_proveedor', raise_exception=True)
def proveedor_nuevo(request):
    form = ProveedorForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        proveedor = form.save()
        messages.success(request, f'Proveedor "{proveedor.nombre}" creado.')
        # Si se llegó desde el formulario de compra, volver a él. Solo ese
        # destino conocido: nunca una URL que venga en la petición.
        if request.POST.get('siguiente') == 'compra':
            return redirect('inventario:compra_nueva')
        return redirect('inventario:proveedor_lista')
    return render(request, 'inventario/proveedor_form.html', {
        'form': form, 'proveedor': None, 'siguiente': request.GET.get('siguiente', ''),
    })


@login_required
@permission_required('inventario.change_proveedor', raise_exception=True)
def proveedor_editar(request, pk):
    proveedor = get_object_or_404(Proveedor, pk=pk)
    form = ProveedorForm(request.POST or None, instance=proveedor)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, f'Proveedor "{proveedor.nombre}" actualizado.')
        return redirect('inventario:proveedor_lista')
    return render(request, 'inventario/proveedor_form.html', {'form': form, 'proveedor': proveedor, 'siguiente': ''})
