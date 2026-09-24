"""Datos disponibles en todas las plantillas (vía TEMPLATES.context_processors),
para elementos de interfaz que aparecen en cualquier pantalla: el contador de
anomalías sin revisar de la barra lateral y el ancla del asistente flotante."""
from .models import Anomalia


def datos_globales(request):
    if not request.user.is_authenticated:
        return {}
    return {
        'anomalias_sin_revisar_total': Anomalia.objects.filter(revisada=False).count(),
    }
