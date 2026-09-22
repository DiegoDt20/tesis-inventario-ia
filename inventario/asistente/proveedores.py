"""Proveedores del modelo de lenguaje del asistente conversacional.

Interfaz común (ProveedorLLM.generar_respuesta) para poder cambiar de
proveedor modificando solo LLM_PROVEEDOR/LLM_MODELO/LLM_URL en el .env, sin
tocar el resto del módulo asistente. Proveedor actual: Ollama local, para
que los datos de la microempresa nunca salgan del equipo. Agregar un
proveedor externo más adelante implica solo una clase nueva aquí más una
entrada en PROVEEDORES.
"""
import json
from abc import ABC, abstractmethod

import requests
from django.conf import settings


class ErrorProveedorLLM(Exception):
    """El proveedor de LLM no respondió (servicio caído, tiempo de espera
    agotado, respuesta inválida). Quien llama debe manejarlo mostrando el
    contexto recuperado sin redactar, no dejar la pantalla rota (ver
    asistente.py)."""


class ProveedorLLM(ABC):
    @abstractmethod
    def generar_respuesta(self, mensajes):
        """`mensajes`: lista de dicts {'role': 'system'|'user'|'assistant',
        'content': str}. Devuelve el texto de la respuesta, o lanza
        ErrorProveedorLLM si el proveedor falla."""

    @abstractmethod
    def generar_respuesta_stream(self, mensajes):
        """Igual que generar_respuesta, pero devuelve un generador que va
        produciendo la respuesta en fragmentos de texto a medida que el
        modelo los genera (para el asistente en tiempo real, ver
        asistente.py:consultar_asistente_stream). Lanza ErrorProveedorLLM si
        el proveedor falla, ya sea antes del primer fragmento o a mitad de
        la generación."""


class ProveedorOllama(ProveedorLLM):
    """Llama a la API local de Ollama (POST /api/chat). Sin clave de API: el
    modelo corre en el propio equipo."""

    def __init__(self, url=None, modelo=None, timeout=60):
        self.url = (url or settings.LLM_URL).rstrip('/')
        self.modelo = modelo or settings.LLM_MODELO
        self.timeout = timeout

    def generar_respuesta(self, mensajes):
        try:
            respuesta = requests.post(
                f'{self.url}/api/chat',
                json={'model': self.modelo, 'messages': mensajes, 'stream': False},
                timeout=self.timeout,
            )
            respuesta.raise_for_status()
            datos = respuesta.json()
        except (requests.RequestException, ValueError) as error:
            raise ErrorProveedorLLM(f'No se pudo contactar a Ollama en {self.url}: {error}') from error

        contenido = (datos.get('message') or {}).get('content', '').strip()
        if not contenido:
            raise ErrorProveedorLLM('Ollama respondió sin contenido.')
        return contenido

    def generar_respuesta_stream(self, mensajes):
        # Con stream=True, Ollama devuelve un objeto JSON por línea (NDJSON):
        # {"message": {"content": "frag"}, "done": false} ... {"done": true}.
        # requests con stream=True no abre la conexión de red hasta el
        # primer iter_lines(), así que un Ollama caído recién falla ahí, no
        # en el post() (por eso el try envuelve todo el generador, no solo
        # la llamada a requests.post).
        hubo_contenido = False
        try:
            respuesta = requests.post(
                f'{self.url}/api/chat',
                json={'model': self.modelo, 'messages': mensajes, 'stream': True},
                timeout=self.timeout, stream=True,
            )
            respuesta.raise_for_status()
            for linea in respuesta.iter_lines():
                if not linea:
                    continue
                fragmento = json.loads(linea)
                contenido = (fragmento.get('message') or {}).get('content', '')
                if contenido:
                    hubo_contenido = True
                    yield contenido
                if fragmento.get('done'):
                    break
        except (requests.RequestException, ValueError) as error:
            raise ErrorProveedorLLM(f'No se pudo contactar a Ollama en {self.url}: {error}') from error

        if not hubo_contenido:
            raise ErrorProveedorLLM('Ollama respondió sin contenido.')


# Único lugar que hay que tocar para agregar un proveedor externo: una clase
# nueva que implemente ProveedorLLM y una entrada aquí.
PROVEEDORES = {
    'ollama': ProveedorOllama,
}


def obtener_proveedor():
    """Devuelve una instancia del proveedor configurado en
    settings.LLM_PROVEEDOR."""
    clase = PROVEEDORES.get(settings.LLM_PROVEEDOR)
    if clase is None:
        raise ValueError(f'Proveedor de LLM no soportado: {settings.LLM_PROVEEDOR!r}')
    return clase()
