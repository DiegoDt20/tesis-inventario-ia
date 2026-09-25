"""Pantalla de recomendaciones de reposición generadas por el motor de
decisiones (inventario/decisiones/)."""
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.paginator import Paginator
from django.db.models import Case, Count, IntegerField, When
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..models import Recomendacion


# Con más días que esto desde la última corrida de "generar_recomendaciones",
# las recomendaciones ya no reflejan el stock ni la demanda actuales.
DIAS_CORRIDA_VIGENTE = 7

# (singular, plural) para el resumen: "51 críticos, 25 reponer, ...".
ETIQUETAS_RESUMEN = {
    Recomendacion.Estado.CRITICO: ('crítico', 'críticos'),
    Recomendacion.Estado.REPONER: ('reponer', 'reponer'),
    Recomendacion.Estado.NORMAL: ('normal', 'normales'),
    Recomendacion.Estado.EXCESO: ('en exceso', 'en exceso'),
}


def _resumen_lote(qs):
    """Conteo por estado de todo el lote (no solo de la página) y costo
    total si se aceptaran todas las críticas. Las críticas cuyo producto no
    tiene costo de compra registrado no entran en el total: se cuentan
    aparte para que el total no parezca completo sin serlo."""
    conteos = dict(qs.values_list('estado').annotate(total=Count('id')))
    estados = []
    for valor, (singular, plural) in ETIQUETAS_RESUMEN.items():
        total = conteos.get(valor, 0)
        estados.append({'valor': valor, 'total': total, 'etiqueta': singular if total == 1 else plural})
    costo_criticas = Decimal('0')
    criticas_sin_costo = 0
    for r in qs.filter(estado=Recomendacion.Estado.CRITICO).select_related('producto'):
        if not r.unidades_sugeridas:
            continue
        if r.costo_estimado is None:
            criticas_sin_costo += 1
        else:
            costo_criticas += r.costo_estimado
    return {
        'estados': estados,
        'total_criticas': conteos.get(Recomendacion.Estado.CRITICO, 0),
        'costo_criticas': costo_criticas,
        'criticas_sin_costo': criticas_sin_costo,
    }


@login_required
@permission_required('inventario.change_recomendacion', raise_exception=True)
def recomendaciones_lista(request):
    ultima_fecha_generacion = (
        Recomendacion.objects.order_by('-fecha_generacion').values_list('fecha_generacion', flat=True).first()
    )
    pagina = resumen = dias_desde_corrida = None
    if ultima_fecha_generacion is not None:
        orden_urgencia = Case(
            When(estado=Recomendacion.Estado.CRITICO, then=0),
            When(estado=Recomendacion.Estado.REPONER, then=1),
            When(estado=Recomendacion.Estado.NORMAL, then=2),
            default=3,
            output_field=IntegerField(),
        )
        lote = Recomendacion.objects.filter(fecha_generacion=ultima_fecha_generacion)
        qs = (
            lote.select_related('producto').annotate(orden_urgencia=orden_urgencia)
            .order_by('orden_urgencia', '-cantidad_sugerida')
        )
        pagina = Paginator(qs, 25).get_page(request.GET.get('page'))
        resumen = _resumen_lote(lote)
        dias_desde_corrida = (timezone.now() - ultima_fecha_generacion).days

    contexto = {
        'pagina': pagina,
        'fecha_generacion': ultima_fecha_generacion,
        'resumen': resumen,
        'dias_desde_corrida': dias_desde_corrida,
        'corrida_desactualizada': dias_desde_corrida is not None and dias_desde_corrida > DIAS_CORRIDA_VIGENTE,
        'dias_corrida_vigente': DIAS_CORRIDA_VIGENTE,
    }
    plantilla = (
        'inventario/_recomendaciones_lista_resultados.html' if request.headers.get('HX-Request') == 'true'
        else 'inventario/recomendaciones_lista.html'
    )
    return render(request, plantilla, contexto)


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
        if request.headers.get('HX-Request') == 'true':
            # La tarjeta ya se vuelve a pintar con el nuevo estado (aceptada/
            # rechazada): eso es la confirmación, no hace falta un mensaje
            # aparte que además quedaría "en cola" para la próxima carga
            # completa de página.
            return render(request, 'inventario/_recomendacion_card.html', {'r': recomendacion})
        messages.success(
            request,
            f'Recomendación de {recomendacion.producto.codigo} marcada como '
            f'{"aceptada" if recomendacion.aceptada else "rechazada"}.',
        )
    return redirect('inventario:recomendaciones_lista')
