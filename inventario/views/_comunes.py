"""Utilidades compartidas por varias pantallas: parseo de la querystring,
resolución del periodo/origen del dashboard y de reportes, y la comparación
de un indicador contra su periodo anterior."""
from datetime import date, datetime, timedelta

from ..models import Origen
from ..servicios.indicadores import rango_disponible


def _parsear_fecha(texto):
    if not texto:
        return None
    try:
        return datetime.strptime(texto, '%Y-%m-%d').date()
    except ValueError:
        return None


def _parsear_origen(texto):
    """'' = todos, o un valor válido de Origen. Cualquier otra cosa se trata
    como "todos" (sin filtrar)."""
    if texto in (Origen.PRUEBA, Origen.REAL):
        return texto
    return None


def _origen_o_defecto(request):
    """"Origen de datos" no es un control visible en ninguna pantalla (el
    sistema siempre opera sobre datos "real"): "prueba" era una herramienta
    de desarrollo para poblar el sistema antes de tener datos reales, y
    mostrarla confundía sin aportar nada al usuario del negocio. El
    parámetro ?origen= de la URL se deja vivo solo para depuración manual;
    sin él, el origen es siempre "real"."""
    origen_param = request.GET.get('origen')
    if origen_param is None:
        return Origen.REAL
    return _parsear_origen(origen_param)


def resolver_rango_periodo(request, origen):
    """Control segmentado del periodo (Hoy / 7 días / 30 días /
    Personalizado), compartido por el dashboard y por reportes.
    "Personalizado" (o su ausencia) cae al comportamiento de siempre: todo
    lo que hay cargado, no una ventana arbitraria que podía dejar pedidos
    reales fuera y mostrar un NS incompleto. Devuelve (fecha_inicio,
    fecha_fin, rango)."""
    hoy = date.today()
    rango = request.GET.get('rango')
    fecha_min_disponible, fecha_max_disponible = rango_disponible(origen=origen)
    if rango == 'hoy':
        fecha_inicio = fecha_fin = hoy
    elif rango == '7d':
        fecha_fin, fecha_inicio = hoy, hoy - timedelta(days=6)
    elif rango == '30d':
        fecha_fin, fecha_inicio = hoy, hoy - timedelta(days=29)
    else:
        rango = 'personalizado'
        fecha_fin = _parsear_fecha(request.GET.get('fecha_fin')) or fecha_max_disponible or hoy
        fecha_inicio = _parsear_fecha(request.GET.get('fecha_inicio')) or fecha_min_disponible or fecha_fin
    if fecha_inicio > fecha_fin:
        fecha_inicio, fecha_fin = fecha_fin, fecha_inicio
    return fecha_inicio, fecha_fin, rango


def _variacion_periodo(valor_actual, valor_anterior, mejor_si_sube=True):
    """Compara un indicador contra el mismo periodo anterior (misma
    duración, inmediatamente antes del rango mostrado). Devuelve None si no
    hay con qué comparar (falta alguno de los dos valores, o el anterior es
    0 y no se puede calcular un porcentaje). Si hay comparación, devuelve
    un dict con la variación en porcentaje (siempre positiva: la dirección
    va aparte) y si esa variación es una mejora o un empeoramiento — EI y
    NS mejoran subiendo, COI mejora bajando, así que la misma flecha hacia
    arriba es buena para uno y mala para otro."""
    if valor_actual is None or valor_anterior is None or valor_anterior == 0:
        return None
    variacion = ((valor_actual - valor_anterior) / abs(valor_anterior)) * 100
    if variacion == 0:
        return {'variacion': 0.0, 'sube': None, 'mejora': None}
    sube = variacion > 0
    return {'variacion': abs(variacion), 'sube': sube, 'mejora': sube == mejor_si_sube}
