"""Utilidades de parseo de la querystring compartidas por varias pantallas."""
from datetime import datetime

from ..models import Origen


def _parsear_fecha(texto):
    if not texto:
        return None
    try:
        return datetime.strptime(texto, '%Y-%m-%d').date()
    except ValueError:
        return None


def _parsear_origen(texto):
    """El selector de origen del dashboard: '' = todos, o un valor válido
    de Origen. Cualquier otra cosa se trata como "todos" (sin filtrar)."""
    if texto in (Origen.PRUEBA, Origen.REAL):
        return texto
    return None
