"""Pantalla de anomalías de control de existencias detectadas por
inventario/ml/anomalias.py."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.paginator import Paginator
from django.db.models import Case, IntegerField, When
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..models import Anomalia


def _anomalias_no_revisadas(origen):
    """Anomalías (diferencias de inventario y movimientos atípicos) que
    todavía nadie marcó como revisadas, ordenadas por severidad (alta
    primero) y luego por fecha de detección más reciente."""
    orden_severidad = Case(
        When(severidad=Anomalia.Severidad.ALTA, then=0),
        When(severidad=Anomalia.Severidad.MEDIA, then=1),
        default=2,
        output_field=IntegerField(),
    )
    qs = Anomalia.objects.filter(revisada=False)
    if origen:
        qs = qs.filter(producto__origen=origen)
    return list(
        qs.select_related('producto').annotate(orden_severidad=orden_severidad)
        .order_by('orden_severidad', '-fecha_deteccion')
    )


@login_required
@permission_required('inventario.change_anomalia', raise_exception=True)
def anomalias_lista(request):
    pagina = Paginator(_anomalias_no_revisadas(None), 25).get_page(request.GET.get('page'))
    return render(request, 'inventario/anomalias_lista.html', {'pagina': pagina})


@login_required
@permission_required('inventario.change_anomalia', raise_exception=True)
@require_POST
def anomalia_marcar_revisada(request, pk):
    anomalia = get_object_or_404(Anomalia, pk=pk)
    anomalia.revisada = True
    anomalia.fecha_revision = timezone.now()
    anomalia.save(update_fields=['revisada', 'fecha_revision'])
    messages.success(request, 'Anomalía marcada como revisada.')
    return redirect('inventario:anomalias_lista')
