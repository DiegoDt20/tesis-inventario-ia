"""Pantalla técnica del modelo de predicción de demanda: el modelo en
producción y el base del que parte, la evaluación vigente del pronóstico
(validación de origen móvil, contra las tres líneas base), el estudio de
ablación y el historial completo de entrenamientos. Documentación de la
investigación para la tesis, no una pantalla de uso diario del negocio —
por eso vive aparte del dashboard.

La evaluación y la ablación se leen de los reportes ya guardados
(servicios/reportes_modelo.py); la pantalla no recalcula nada al cargar.

Arriba va un resumen sin jerga para el dueño (la exactitud en la ventana de
reposición, con semáforo por categoría); todo el detalle técnico queda en
una sección plegada, cerrada en cada visita."""
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Max, Min
from django.shortcuts import render

from ..models import ModeloEntrenado, Origen, PedidoDetalle
from ..servicios.reportes_modelo import ablacion_vigente, evaluacion_vigente, resumen_pronostico


def _dias_historico(origen):
    detalles = PedidoDetalle.objects.all()
    if origen:
        detalles = detalles.filter(pedido__origen=origen)
    rango = detalles.aggregate(minimo=Min('pedido__fecha_solicitud'), maximo=Max('pedido__fecha_solicitud'))
    if rango['minimo'] and rango['maximo']:
        return (rango['maximo'].date() - rango['minimo'].date()).days + 1
    return None


def _modelos_vigentes():
    """El modelo en producción (el ajustado activo, el mismo que toma
    predecir_demanda) y el base vigente del que parte el ajuste. Cumplen
    funciones distintas: no compiten por el mismo puesto."""
    produccion = ModeloEntrenado.objects.filter(fase=ModeloEntrenado.Fase.AJUSTADO, activo=True).first()
    base = ModeloEntrenado.objects.filter(fase=ModeloEntrenado.Fase.BASE, activo=True).first()
    return produccion, base


@login_required
def modelo_detalle(request):
    produccion, base = _modelos_vigentes()
    escaladas = {}
    if produccion and produccion.escalado:
        escaladas = {s: f for s, f in produccion.escalado.get('factores', {}).items() if f != 1.0}
    pagina = Paginator(ModeloEntrenado.objects.order_by('-fecha_entrenamiento'), 20).get_page(request.GET.get('page'))
    evaluacion = evaluacion_vigente(produccion)
    contexto = {
        'resumen': resumen_pronostico(evaluacion),
        # Cerrado en cada visita; solo queda abierto al moverse por las
        # páginas del historial, que está dentro del detalle.
        'detalle_abierto': 'page' in request.GET,
        'produccion': produccion,
        'base': base,
        'series_escaladas': escaladas,
        'dias_historico': _dias_historico(Origen.REAL),
        'evaluacion': evaluacion,
        'ablacion': ablacion_vigente(),
        'pagina': pagina,
    }
    return render(request, 'inventario/modelo.html', contexto)
