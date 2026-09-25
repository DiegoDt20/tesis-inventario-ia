"""Pantallas de registro y listado de conteos físicos."""
from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import redirect, render
from django.urls import reverse

from ..models import ConteoDetalle, ConteoFisico, Origen, Producto
from ._comunes import _parsear_fecha


@login_required
def conteo_lista(request):
    """Un conteo por fila (no un producto por fila): cuántos productos se
    contaron y cuántos quedaron con diferencia, para ver de un vistazo qué
    conteos anteriores hay y continuar cualquiera desde aquí."""
    qs = ConteoFisico.objects.annotate(
        total_contados=Count('detalles'),
        total_con_diferencia=Count('detalles', filter=~Q(detalles__diferencia=0)),
    ).order_by('-fecha_corte', '-pk')
    pagina = Paginator(qs, 25).get_page(request.GET.get('page'))
    return render(request, 'inventario/conteo_lista.html', {'pagina': pagina})


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
