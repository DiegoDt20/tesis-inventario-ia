"""Catálogo de productos: listado con búsqueda, alta y edición. El stock
no se edita aquí (solo cambia con movimientos y recepción de compras)."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render

from ..forms import ProductoForm
from ..models import Categoria, Origen, Producto


def _querystring_sin_pagina(request):
    parametros = request.GET.copy()
    parametros.pop('page', None)
    return parametros.urlencode()


@login_required
@permission_required('inventario.view_producto', raise_exception=True)
def producto_lista(request):
    texto = (request.GET.get('q') or '').strip()
    categoria = request.GET.get('categoria') or ''
    # Por defecto solo los activos (el catálogo vigente); "inactivos" y
    # "todos" son para revisar lo que se dio de baja.
    estado = request.GET.get('estado') or 'activos'

    qs = Producto.objects.all().order_by('nombre', 'codigo')
    if texto:
        qs = qs.filter(Q(codigo__icontains=texto) | Q(nombre__icontains=texto))
    if categoria in Categoria.values:
        qs = qs.filter(categoria=categoria)
    if estado == 'activos':
        qs = qs.filter(activo=True)
    elif estado == 'inactivos':
        qs = qs.filter(activo=False)

    return render(request, 'inventario/producto_lista.html', {
        'pagina': Paginator(qs, 25).get_page(request.GET.get('page')),
        'total': qs.count(),
        'q': texto,
        'categoria': categoria,
        'estado': estado,
        'categorias': Categoria.choices,
        'querystring': _querystring_sin_pagina(request),
    })


@login_required
@permission_required('inventario.add_producto', raise_exception=True)
def producto_nuevo(request):
    form = ProductoForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        producto = form.save(commit=False)
        # Como todo lo que se registra desde las pantallas: dato real.
        producto.origen = Origen.REAL
        producto.save()
        messages.success(request, f'Producto {producto.codigo} creado. Su stock empieza en 0: regístralo con un movimiento.')
        return redirect('inventario:producto_lista')
    return render(request, 'inventario/producto_form.html', {'form': form, 'producto': None})


@login_required
@permission_required('inventario.change_producto', raise_exception=True)
def producto_editar(request, pk):
    producto = get_object_or_404(Producto, pk=pk)
    form = ProductoForm(request.POST or None, instance=producto)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, f'Producto {producto.codigo} actualizado.')
        return redirect('inventario:producto_lista')
    return render(request, 'inventario/producto_form.html', {'form': form, 'producto': producto})
