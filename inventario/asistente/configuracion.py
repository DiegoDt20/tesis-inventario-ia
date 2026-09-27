"""Configuración del asistente conversacional leída de settings/.env."""
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from inventario.models import Origen


def origen_asistente():
    """Origen de datos (settings.ASISTENTE_ORIGEN, "real" por defecto) con
    el que el asistente indexa y responde: el mismo que usa el dashboard,
    para que los indicadores no mezclen datos de prueba con reales."""
    origen = (settings.ASISTENTE_ORIGEN or '').strip().lower()
    if origen not in Origen.values:
        raise ImproperlyConfigured(
            f'ASISTENTE_ORIGEN debe ser uno de {Origen.values}; en el .env dice {settings.ASISTENTE_ORIGEN!r}.'
        )
    return origen
