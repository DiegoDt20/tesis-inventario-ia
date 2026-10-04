"""Filtros de plantilla compartidos por las pantallas de inventario."""
from django import template
from django.utils.formats import number_format

register = template.Library()

# Un solo mapeo para todo el sistema: verde = atendido/normal/correcto,
# ámbar = pendiente/reponer/severidad media, rojo = crítico/no atendido/
# severidad alta, gris = sin datos o estados neutros. Cualquier pantalla
# que muestre un estado debe pasar por este filtro en vez de repetir su
# propio if/elif, para que el color de un mismo estado nunca cambie de una
# pantalla a otra.
_MAPA_COLORES = {
    'atendido': 'success',
    'normal': 'success',
    'correcto': 'success',
    'si': 'success',
    'true': 'success',
    'pendiente': 'warning',
    'reponer': 'warning',
    'media': 'warning',
    'critico': 'danger',
    'alta': 'danger',
    'no': 'danger',
    'false': 'danger',
    'con_diferencia': 'danger',
    'exceso': 'secondary',
    'cancelado': 'secondary',
    'baja': 'secondary',
    'otro': 'secondary',
    'sin_datos': 'secondary',
    'sin_contar': 'secondary',
}


def _clave(valor):
    if isinstance(valor, bool):
        return 'si' if valor else 'no'
    if valor is None:
        return 'sin_datos'
    return str(valor).strip().lower()


@register.filter
def color_estado(valor):
    """Traduce un estado/severidad de negocio (string, o booleano como
    atendido_a_tiempo) al color Bootstrap que le corresponde en todo el
    sistema. Un valor desconocido se trata como neutro (gris), nunca como
    error de plantilla."""
    return _MAPA_COLORES.get(_clave(valor), 'secondary')


# Un icono de Bootstrap Icons por cada mismo color de _MAPA_COLORES: así el
# badge siempre lleva un ícono coherente con su significado (una marca para
# lo positivo, un triángulo para lo que hay que vigilar, una alerta para lo
# urgente, un guion para lo neutro), sin importar de qué pantalla venga.
_ICONOS_POR_COLOR = {
    'success': 'bi-check-circle-fill',
    'warning': 'bi-exclamation-triangle-fill',
    'danger': 'bi-exclamation-octagon-fill',
    'secondary': 'bi-dash-circle-fill',
}


@register.filter
def icono_estado(valor):
    """Ícono de Bootstrap Icons que acompaña al badge de color_estado para
    el mismo valor de estado/severidad."""
    color = _MAPA_COLORES.get(_clave(valor), 'secondary')
    return _ICONOS_POR_COLOR[color]


@register.filter
def porcentaje(valor, decimales=1):
    """Fracción como porcentaje con el formato local (0.164 → "16,4 %"), o
    un guion si no está definida."""
    if valor is None or valor == '':
        return '—'
    # number_format trunca los decimales sobrantes: se redondea antes. El
    # espacio antes de % no se parte entre líneas.
    return f'{number_format(round(float(valor) * 100, int(decimales)), int(decimales))}\u00a0%'


@register.filter
def porcentaje_signo(valor, decimales=1):
    """Como porcentaje, con el signo explícito: para sesgos (+31,3 % es
    sobreestimación, −3,8 % subestimación)."""
    if valor is None or valor == '':
        return '—'
    signo = '+' if float(valor) > 0 else ('−' if float(valor) < 0 else '')
    return f'{signo}{number_format(round(abs(float(valor)) * 100, int(decimales)), int(decimales))}\u00a0%'
