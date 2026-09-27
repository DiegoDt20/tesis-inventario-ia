"""Anonimización de datos antes de enviar cualquier información a la API del
modelo de lenguaje (requisito del código de ética de la universidad): nunca
se envían nombres de clientes, la razón social de la microempresa ni otros
datos identificables. Solo códigos de producto, cantidades, fechas y
categorías.

Se aplica siempre, sin importar el proveedor de LLM configurado. Con un
proveedor externo (Anthropic) es obligatoria, porque los datos salen del
equipo: además de asistente.py, ProveedorAnthropic vuelve a aplicarla a
cada mensaje justo antes de enviarlo (ver proveedores.py).
"""
import re

from django.conf import settings

from inventario.models import Pedido

_PATRON_EMAIL = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')
# Secuencia de 7 a 15 dígitos sin separadores (teléfonos): las fechas que
# arma el resto del sistema siempre llevan separador (dd/mm/aaaa, aaaa-mm),
# así que nunca caen en este patrón.
_PATRON_TELEFONO = re.compile(r'(?<!\d)\+?\d{7,15}(?!\d)')

_MARCADOR_CLIENTE = '[CLIENTE]'
_MARCADOR_EMPRESA = '[EMPRESA]'
_MARCADOR_CORREO = '[CORREO]'
_MARCADOR_TELEFONO = '[TELEFONO]'


def _nombres_clientes():
    """Nombres distintos registrados en Pedido.cliente, para poder
    redactarlos si aparecen en un texto a enviar."""
    # order_by() vacío: el ordering por defecto de Pedido entra en el
    # SELECT DISTINCT y devolvería el mismo nombre repetido.
    return [
        nombre for nombre in Pedido.objects.order_by().values_list('cliente', flat=True).distinct()
        if nombre and nombre.strip()
    ]


def anonimizar_texto(texto):
    """Redacta de `texto` nombres de clientes registrados, la razón social
    de la microempresa (si está configurada), correos y teléfonos. Devuelve
    el texto anonimizado; nunca modifica códigos de producto, cantidades,
    fechas ni categorías."""
    if not texto:
        return texto

    resultado = texto
    # Sin distinguir mayúsculas: el usuario puede escribir "c-050" o
    # "juan pérez" en la pregunta, no exactamente como quedó registrado.
    # Los nombres más largos primero, para que uno que contiene a otro no
    # quede redactado a medias.
    for nombre in sorted(_nombres_clientes(), key=len, reverse=True):
        resultado = re.sub(re.escape(nombre.strip()), _MARCADOR_CLIENTE, resultado, flags=re.IGNORECASE)

    razon_social = (getattr(settings, 'EMPRESA_RAZON_SOCIAL', '') or '').strip()
    if razon_social:
        resultado = re.sub(re.escape(razon_social), _MARCADOR_EMPRESA, resultado, flags=re.IGNORECASE)

    resultado = _PATRON_EMAIL.sub(_MARCADOR_CORREO, resultado)
    resultado = _PATRON_TELEFONO.sub(_MARCADOR_TELEFONO, resultado)
    return resultado
