"""Pantalla de recomendaciones de reposición generadas por el motor de
decisiones (inventario/decisiones/)."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.paginator import Paginator
from django.db.models import Case, IntegerField, When
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..models import Recomendacion


@login_required
@permission_required('inventario.change_recomendacion', raise_exception=True)
def recomendaciones_lista(request):
    ultima_fecha_generacion = (
        Recomendacion.objects.order_by('-fecha_generacion').values_list('fecha_generacion', flat=True).first()
    )
    pagina = None
    if ultima_fecha_generacion is not None:
        orden_urgencia = Case(
            When(estado=Recomendacion.Estado.CRITICO, then=0),
            When(estado=Recomendacion.Estado.REPONER, then=1),
            When(estado=Recomendacion.Estado.NORMAL, then=2),
            default=3,
            output_field=IntegerField(),
        )
        qs = (
            Recomendacion.objects.filter(fecha_generacion=ultima_fecha_generacion)
            .select_related('producto').annotate(orden_urgencia=orden_urgencia)
            .order_by('orden_urgencia', '-cantidad_sugerida')
        )
        pagina = Paginator(qs, 25).get_page(request.GET.get('page'))

    contexto = {'pagina': pagina, 'fecha_generacion': ultima_fecha_generacion}
    return render(request, 'inventario/recomendaciones_lista.html', contexto)


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
        messages.success(
            request,
            f'Recomendación de {recomendacion.producto.codigo} marcada como '
            f'{"aceptada" if recomendacion.aceptada else "rechazada"}.',
        )
    return redirect('inventario:recomendaciones_lista')
