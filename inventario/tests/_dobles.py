"""Dobles de prueba compartidos por varios archivos de tests."""
from unittest.mock import MagicMock

from ..asistente.proveedores import ProveedorLLM


def _proveedor_simulado(nombre='ollama', modelo='qwen2.5:7b'):
    """Proveedor de LLM simulado con la interfaz de ProveedorLLM, incluidos
    los atributos que asistente.py registra en ConsultaAsistente. Cada test
    configura generar_respuesta / generar_respuesta_stream a su gusto."""
    proveedor = MagicMock(spec=ProveedorLLM)
    proveedor.nombre = nombre
    proveedor.modelo = modelo
    proveedor.modelo_respondio = None
    return proveedor
