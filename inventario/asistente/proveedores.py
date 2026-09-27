"""Proveedores del modelo de lenguaje del asistente conversacional.

Interfaz común (ProveedorLLM.generar_respuesta) para poder cambiar de
proveedor modificando solo LLM_PROVEEDOR/LLM_MODELO (y LLM_URL o
ANTHROPIC_API_KEY) en el .env, sin tocar el resto del módulo asistente:

- "ollama": modelo local; los datos de la microempresa no salen del equipo.
- "anthropic": Claude vía la API de Anthropic; los datos SÍ salen del
  equipo hacia un servicio externo, así que ProveedorAnthropic vuelve a
  anonimizar cada mensaje justo antes de enviarlo (ver _anonimizar_mensajes),
  aunque asistente.py ya los mande anonimizados.

Después de cada llamada exitosa, `modelo_respondio` guarda el modelo que
informó el propio servicio, para registrarlo en ConsultaAsistente.
"""
import json
import logging
from abc import ABC, abstractmethod

import anthropic
import requests
from django.conf import settings

from .anonimizador import anonimizar_texto

logger = logging.getLogger(__name__)


class ErrorProveedorLLM(Exception):
    """El proveedor de LLM no respondió (servicio caído, tiempo de espera
    agotado, respuesta inválida). Quien llama debe manejarlo mostrando el
    contexto recuperado sin redactar, no dejar la pantalla rota (ver
    asistente.py)."""


class ProveedorLLM(ABC):
    # Nombre con el que se configura en LLM_PROVEEDOR y se registra en
    # ConsultaAsistente.proveedor_llm.
    nombre = None

    def __init__(self, modelo=None):
        self.modelo = modelo or settings.LLM_MODELO
        # Modelo que efectivamente respondió según el servicio (puede
        # diferir del configurado si este es un alias); None hasta que
        # haya una respuesta.
        self.modelo_respondio = None

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

    nombre = 'ollama'

    def __init__(self, url=None, modelo=None, timeout=60):
        super().__init__(modelo)
        self.url = (url or settings.LLM_URL).rstrip('/')
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
        self.modelo_respondio = datos.get('model') or self.modelo
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
                    if not hubo_contenido:
                        self.modelo_respondio = fragmento.get('model') or self.modelo
                    hubo_contenido = True
                    yield contenido
                if fragmento.get('done'):
                    break
        except (requests.RequestException, ValueError) as error:
            raise ErrorProveedorLLM(f'No se pudo contactar a Ollama en {self.url}: {error}') from error

        if not hubo_contenido:
            raise ErrorProveedorLLM('Ollama respondió sin contenido.')


class ProveedorAnthropic(ProveedorLLM):
    """Claude vía la API de Anthropic (SDK oficial `anthropic`). La clave
    sale de settings.ANTHROPIC_API_KEY (.env), nunca del código.

    Cualquier falla (sin clave, sin conexión, tiempo de espera, error HTTP
    de la API, respuesta vacía) se registra en el log (con el código HTTP
    cuando lo hay) y se convierte en ErrorProveedorLLM, para que
    asistente.py aplique el mismo respaldo que con Ollama: mostrar los datos
    recuperados sin redactar."""

    nombre = 'anthropic'

    # Las respuestas del asistente son breves por diseño (PROMPT_SISTEMA);
    # este tope solo evita un gasto desbocado si el modelo se extendiera.
    MAX_TOKENS = 4096

    def __init__(self, modelo=None, api_key=None, timeout=60, max_reintentos=1):
        super().__init__(modelo)
        self.api_key = api_key if api_key is not None else settings.ANTHROPIC_API_KEY
        self.timeout = timeout
        # El SDK reintenta 429/5xx/conexión por su cuenta; con más de un
        # reintento la pantalla tardaría minutos en caer al respaldo.
        self.max_reintentos = max_reintentos

    def _cliente(self):
        if not self.api_key:
            logger.error('LLM_PROVEEDOR=anthropic pero ANTHROPIC_API_KEY está vacía en el .env.')
            raise ErrorProveedorLLM('Falta ANTHROPIC_API_KEY en el .env.')
        return anthropic.Anthropic(api_key=self.api_key, timeout=self.timeout, max_retries=self.max_reintentos)

    @staticmethod
    def _anonimizar_mensajes(mensajes):
        """Separa el prompt de sistema (la API de Anthropic lo recibe aparte,
        no como un mensaje con role "system") y vuelve a anonimizar TODO lo
        que se va a enviar. Es la última barrera antes de que los datos
        salgan del equipo: no depende de que quien llama haya anonimizado."""
        sistema = '\n\n'.join(anonimizar_texto(m['content']) for m in mensajes if m['role'] == 'system')
        conversacion = [
            {'role': m['role'], 'content': anonimizar_texto(m['content'])}
            for m in mensajes if m['role'] != 'system'
        ]
        return sistema, conversacion

    def _parametros(self, mensajes):
        sistema, conversacion = self._anonimizar_mensajes(mensajes)
        parametros = {'model': self.modelo, 'max_tokens': self.MAX_TOKENS, 'messages': conversacion}
        if sistema:
            parametros['system'] = sistema
        return parametros

    def _registrar_y_convertir(self, error):
        """Registra la falla en el log y devuelve el ErrorProveedorLLM a
        lanzar. Los errores HTTP de la API traen el código de respuesta y el
        request-id (útil para reportar el caso a Anthropic)."""
        if isinstance(error, anthropic.APIStatusError):
            request_id = error.response.headers.get('request-id') if error.response is not None else None
            logger.error(
                'La API de Anthropic respondió con error HTTP %s (modelo %s, request-id %s): %s',
                error.status_code, self.modelo, request_id, error.message,
            )
            return ErrorProveedorLLM(f'La API de Anthropic respondió con error HTTP {error.status_code}.')
        if isinstance(error, anthropic.APIConnectionError):
            # Incluye APITimeoutError: no hubo respuesta, así que no hay código HTTP.
            logger.error('No se pudo conectar con la API de Anthropic (modelo %s): %s', self.modelo, error)
            return ErrorProveedorLLM('No se pudo conectar con la API de Anthropic.')
        logger.error('Falla inesperada de la API de Anthropic (modelo %s): %s', self.modelo, error)
        return ErrorProveedorLLM(f'Falla de la API de Anthropic: {error}')

    def _validar_final(self, mensaje, texto):
        if mensaje.stop_reason == 'refusal':
            logger.error('La API de Anthropic rechazó la consulta (modelo %s, stop_reason=refusal).', self.modelo)
            raise ErrorProveedorLLM('La API de Anthropic rechazó la consulta.')
        if not texto.strip():
            logger.error('La API de Anthropic respondió sin texto (modelo %s, stop_reason=%s).',
                         self.modelo, mensaje.stop_reason)
            raise ErrorProveedorLLM('La API de Anthropic respondió sin contenido.')
        self.modelo_respondio = mensaje.model

    def generar_respuesta(self, mensajes):
        try:
            mensaje = self._cliente().messages.create(**self._parametros(mensajes))
        except anthropic.APIError as error:
            raise self._registrar_y_convertir(error) from error
        texto = ''.join(bloque.text for bloque in mensaje.content if bloque.type == 'text').strip()
        self._validar_final(mensaje, texto)
        return texto

    def generar_respuesta_stream(self, mensajes):
        cliente = self._cliente()
        partes = []
        try:
            with cliente.messages.stream(**self._parametros(mensajes)) as stream:
                for texto in stream.text_stream:
                    if texto:
                        partes.append(texto)
                        yield texto
                mensaje = stream.get_final_message()
        except anthropic.APIError as error:
            raise self._registrar_y_convertir(error) from error
        self._validar_final(mensaje, ''.join(partes))


# Único lugar que hay que tocar para agregar un proveedor: una clase nueva
# que implemente ProveedorLLM y una entrada aquí.
PROVEEDORES = {
    ProveedorOllama.nombre: ProveedorOllama,
    ProveedorAnthropic.nombre: ProveedorAnthropic,
}


def obtener_proveedor():
    """Devuelve una instancia del proveedor configurado en
    settings.LLM_PROVEEDOR."""
    clase = PROVEEDORES.get((settings.LLM_PROVEEDOR or '').strip().lower())
    if clase is None:
        raise ValueError(f'Proveedor de LLM no soportado: {settings.LLM_PROVEEDOR!r}')
    return clase()
