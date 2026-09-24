"""Buscador global de la barra superior: una vista de solo lectura que junta
coincidencias de productos, pedidos y movimientos. No es una pantalla de
navegación (Producto y Movimiento no tienen una vista de detalle propia
todavía), así que los resultados se muestran como una vista previa
informativa en el propio desplegable, sin enlazar a páginas que no existen."""
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import render

from ..models import Movimiento, Pedido, Producto

LIMITE_POR_TIPO = 5


@login_required
def busqueda_global(request):
    texto = request.GET.get('q', '').strip()
    contexto = {'texto': texto, 'productos': [], 'pedidos': [], 'movimientos': []}

    if len(texto) >= 2:
        contexto['productos'] = list(
            Producto.objects.filter(Q(codigo__icontains=texto) | Q(nombre__icontains=texto), activo=True)[:LIMITE_POR_TIPO]
        )
        contexto['pedidos'] = list(
            Pedido.objects.filter(cliente__icontains=texto).order_by('-fecha_solicitud')[:LIMITE_POR_TIPO]
        )
        contexto['movimientos'] = list(
            Movimiento.objects.filter(
                Q(documento__icontains=texto) | Q(producto__codigo__icontains=texto) | Q(producto__nombre__icontains=texto)
            ).select_related('producto').order_by('-fecha')[:LIMITE_POR_TIPO]
        )

    return render(request, 'inventario/_busqueda_resultados.html', contexto)
