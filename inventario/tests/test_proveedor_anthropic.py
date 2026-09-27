"""Tests del proveedor Anthropic (Claude) del asistente conversacional, con
la API simulada: nunca se hace una petición real ni se usa una clave real.

Cubren que se use el proveedor configurado, que lo que sale hacia la API
esté anonimizado (y sea solo el prompt de sistema, el contexto y la
pregunta), que una falla de la API caiga al respaldo sin romper la pantalla
y quede en el log con su código HTTP, y que ConsultaAsistente registre el
proveedor y el modelo."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import anthropic
import httpx2
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from inventario.asistente import proveedores
from inventario.asistente.asistente import PROMPT_SISTEMA, consultar_asistente
from inventario.asistente.proveedores import (
    ErrorProveedorLLM,
    ProveedorAnthropic,
    ProveedorOllama,
    obtener_proveedor,
)
from inventario.models import ConsultaAsistente, DocumentoIndexado, Pedido, TipoDocumento

CONFIG_ANTHROPIC = {
    'LLM_PROVEEDOR': 'anthropic',
    'LLM_MODELO': 'claude-haiku-4-5-20251001',
    'ANTHROPIC_API_KEY': 'clave-de-prueba',
}
MODELO_RESPUESTA = 'claude-haiku-4-5-20251001'
URL_API = 'https://api.anthropic.com/v1/messages'


def _vector():
    return [1.0] + [0.0] * 383


def _mensaje(texto, stop_reason='end_turn'):
    """Imita el Message que devuelve el SDK: solo lo que usa el proveedor."""
    bloques = [SimpleNamespace(type='text', text=texto)] if texto else []
    return SimpleNamespace(content=bloques, stop_reason=stop_reason, model=MODELO_RESPUESTA)


def _stream(fragmentos, stop_reason='end_turn'):
    """Imita el context manager de client.messages.stream()."""
    stream = MagicMock()
    stream.text_stream = iter(fragmentos)
    stream.get_final_message.return_value = _mensaje(''.join(fragmentos), stop_reason)
    administrador = MagicMock()
    administrador.__enter__.return_value = stream
    administrador.__exit__.return_value = False
    return administrador


def _error_http(codigo):
    respuesta = httpx2.Response(
        codigo, request=httpx2.Request('POST', URL_API), headers={'request-id': 'req_prueba_123'},
    )
    return anthropic.APIStatusError('Servicio sobrecargado', response=respuesta, body=None)


@override_settings(**CONFIG_ANTHROPIC)
class SeleccionDeProveedorTests(TestCase):
    def test_usa_el_proveedor_configurado(self):
        proveedor = obtener_proveedor()
        self.assertIsInstance(proveedor, ProveedorAnthropic)
        self.assertEqual(proveedor.modelo, 'claude-haiku-4-5-20251001')

    @override_settings(LLM_PROVEEDOR=' Anthropic ')
    def test_tolera_mayusculas_y_espacios_en_el_env(self):
        self.assertIsInstance(obtener_proveedor(), ProveedorAnthropic)

    @override_settings(LLM_PROVEEDOR='ollama', LLM_MODELO='qwen2.5:7b')
    def test_ollama_sigue_disponible(self):
        self.assertIsInstance(obtener_proveedor(), ProveedorOllama)

    def test_la_clave_sale_del_env_y_se_pasa_al_cliente(self):
        with patch.object(proveedores.anthropic, 'Anthropic') as clase_cliente:
            clase_cliente.return_value.messages.create.return_value = _mensaje('Hola.')
            obtener_proveedor().generar_respuesta([{'role': 'user', 'content': 'hola'}])
        self.assertEqual(clase_cliente.call_args.kwargs['api_key'], 'clave-de-prueba')


@override_settings(**CONFIG_ANTHROPIC)
class ProveedorAnthropicTests(TestCase):
    def setUp(self):
        Pedido.objects.create(
            fecha_solicitud=timezone.now(), cliente='Juan Pérez', canal=Pedido.Canal.MOSTRADOR,
        )
        self.mensajes_sin_anonimizar = [
            {'role': 'system', 'content': 'Eres un asistente.'},
            {'role': 'user', 'content': 'Juan Pérez (juan@correo.pe, 987654321) pidió 5 u. de PIN-002.'},
        ]

    def test_respuesta_y_modelo_que_respondio(self):
        with patch.object(proveedores.anthropic, 'Anthropic') as clase_cliente:
            clase_cliente.return_value.messages.create.return_value = _mensaje('El EI es 92%.')
            proveedor = ProveedorAnthropic()
            texto = proveedor.generar_respuesta(self.mensajes_sin_anonimizar)
        self.assertEqual(texto, 'El EI es 92%.')
        self.assertEqual(proveedor.modelo_respondio, MODELO_RESPUESTA)

    def test_reanonimiza_antes_de_enviar_aunque_quien_llama_no_lo_haya_hecho(self):
        with patch.object(proveedores.anthropic, 'Anthropic') as clase_cliente:
            clase_cliente.return_value.messages.create.return_value = _mensaje('Listo.')
            ProveedorAnthropic().generar_respuesta(self.mensajes_sin_anonimizar)

        enviado = clase_cliente.return_value.messages.create.call_args.kwargs
        serializado = json.dumps(enviado, ensure_ascii=False)
        for dato in ('Juan Pérez', 'juan@correo.pe', '987654321'):
            self.assertNotIn(dato, serializado)
        self.assertIn('[CLIENTE] ([CORREO], [TELEFONO]) pidió 5 u. de PIN-002.', serializado)
        # El prompt de sistema va aparte, como pide la API de Anthropic.
        self.assertEqual(enviado['system'], 'Eres un asistente.')
        self.assertEqual([m['role'] for m in enviado['messages']], ['user'])

    def test_stream_entrega_los_fragmentos(self):
        with patch.object(proveedores.anthropic, 'Anthropic') as clase_cliente:
            clase_cliente.return_value.messages.stream.return_value = _stream(['El EI ', 'es 92%.'])
            proveedor = ProveedorAnthropic()
            fragmentos = list(proveedor.generar_respuesta_stream(self.mensajes_sin_anonimizar))
        self.assertEqual(fragmentos, ['El EI ', 'es 92%.'])
        self.assertEqual(proveedor.modelo_respondio, MODELO_RESPUESTA)
        enviado = json.dumps(clase_cliente.return_value.messages.stream.call_args.kwargs, ensure_ascii=False)
        self.assertNotIn('Juan Pérez', enviado)

    def test_error_http_se_registra_en_el_log_con_el_codigo(self):
        with patch.object(proveedores.anthropic, 'Anthropic') as clase_cliente, \
                self.assertLogs('inventario.asistente.proveedores', level='ERROR') as registro:
            clase_cliente.return_value.messages.create.side_effect = _error_http(529)
            with self.assertRaises(ErrorProveedorLLM):
                ProveedorAnthropic().generar_respuesta(self.mensajes_sin_anonimizar)
        self.assertIn('error HTTP 529', registro.output[0])
        self.assertIn('req_prueba_123', registro.output[0])

    def test_error_http_en_stream_tambien_se_registra(self):
        with patch.object(proveedores.anthropic, 'Anthropic') as clase_cliente, \
                self.assertLogs('inventario.asistente.proveedores', level='ERROR') as registro:
            clase_cliente.return_value.messages.stream.side_effect = _error_http(500)
            with self.assertRaises(ErrorProveedorLLM):
                list(ProveedorAnthropic().generar_respuesta_stream(self.mensajes_sin_anonimizar))
        self.assertIn('error HTTP 500', registro.output[0])

    def test_sin_conexion_se_registra_sin_codigo(self):
        with patch.object(proveedores.anthropic, 'Anthropic') as clase_cliente, \
                self.assertLogs('inventario.asistente.proveedores', level='ERROR') as registro:
            clase_cliente.return_value.messages.create.side_effect = anthropic.APIConnectionError(
                request=httpx2.Request('POST', URL_API),
            )
            with self.assertRaises(ErrorProveedorLLM):
                ProveedorAnthropic().generar_respuesta(self.mensajes_sin_anonimizar)
        self.assertIn('No se pudo conectar', registro.output[0])

    @override_settings(ANTHROPIC_API_KEY='')
    def test_sin_clave_falla_sin_llamar_a_la_api(self):
        with patch.object(proveedores.anthropic, 'Anthropic') as clase_cliente, \
                self.assertLogs('inventario.asistente.proveedores', level='ERROR'):
            with self.assertRaises(ErrorProveedorLLM):
                ProveedorAnthropic().generar_respuesta(self.mensajes_sin_anonimizar)
        clase_cliente.assert_not_called()

    def test_rechazo_o_respuesta_vacia_cuentan_como_falla(self):
        with patch.object(proveedores.anthropic, 'Anthropic') as clase_cliente, \
                self.assertLogs('inventario.asistente.proveedores', level='ERROR'):
            clase_cliente.return_value.messages.create.return_value = _mensaje('', stop_reason='refusal')
            with self.assertRaises(ErrorProveedorLLM):
                ProveedorAnthropic().generar_respuesta(self.mensajes_sin_anonimizar)


@override_settings(**CONFIG_ANTHROPIC)
class AsistenteConAnthropicTests(TestCase):
    """De punta a punta: pregunta -> contexto recuperado -> lo que recibe la
    API simulada -> lo que ve el usuario y lo que queda registrado."""

    def setUp(self):
        Pedido.objects.create(
            fecha_solicitud=timezone.now(), cliente='Juan Pérez', canal=Pedido.Canal.MOSTRADOR,
        )
        self.documento = DocumentoIndexado.objects.create(
            tipo=TipoDocumento.PRODUCTO, referencia_id=1,
            contenido='Pedido reciente de Juan Pérez: 5 unidades de PIN-002. Stock actual: 3.',
            embedding=_vector(),
        )
        User.objects.create_user(username='u1', password='pass12345')
        self.client.login(username='u1', password='pass12345')

    def _recuperar(self):
        return patch('inventario.asistente.asistente.recuperar_documentos', return_value=[self.documento])

    def test_solo_se_envia_el_prompt_el_contexto_y_la_pregunta_anonimizados(self):
        with self._recuperar(), patch.object(proveedores.anthropic, 'Anthropic') as clase_cliente:
            clase_cliente.return_value.messages.create.return_value = _mensaje('Tiene 3 unidades.')
            resultado = consultar_asistente('¿qué le vendimos a juan pérez?')

        enviado = clase_cliente.return_value.messages.create.call_args.kwargs
        self.assertEqual(set(enviado), {'model', 'max_tokens', 'system', 'messages'})
        self.assertEqual(enviado['system'], PROMPT_SISTEMA)
        self.assertEqual(len(enviado['messages']), 1)
        contenido = enviado['messages'][0]['content']
        self.assertEqual(
            contenido,
            'CONTEXTO:\n- Pedido reciente de [CLIENTE]: 5 unidades de PIN-002. Stock actual: 3.\n\n'
            'PREGUNTA: ¿qué le vendimos a [CLIENTE]?',
        )
        self.assertEqual((resultado.proveedor, resultado.modelo), ('anthropic', MODELO_RESPUESTA))

    def test_pantalla_no_se_rompe_y_registra_la_falla(self):
        with self._recuperar(), patch.object(proveedores.anthropic, 'Anthropic') as clase_cliente, \
                self.assertLogs('inventario.asistente.proveedores', level='ERROR'):
            clase_cliente.return_value.messages.create.side_effect = _error_http(529)
            respuesta = self.client.post(
                reverse('inventario:asistente_chat'), {'pregunta': '¿cuánto stock hay?'}, follow=True,
            )

        self.assertEqual(respuesta.status_code, 200)
        consulta = ConsultaAsistente.objects.get()
        # Respaldo: los datos recuperados, sin redactar (no salen del equipo).
        self.assertIn('Pedido reciente de Juan Pérez', consulta.respuesta)
        self.assertEqual(
            (consulta.proveedor_llm, consulta.modelo_llm, consulta.fallo_llm),
            ('anthropic', 'claude-haiku-4-5-20251001', True),
        )

    def test_registra_proveedor_y_modelo_que_respondio(self):
        with self._recuperar(), patch.object(proveedores.anthropic, 'Anthropic') as clase_cliente:
            clase_cliente.return_value.messages.create.return_value = _mensaje('Hay 3 unidades.')
            self.client.post(reverse('inventario:asistente_chat'), {'pregunta': '¿cuánto stock hay?'})

        consulta = ConsultaAsistente.objects.get()
        self.assertEqual(
            (consulta.respuesta, consulta.proveedor_llm, consulta.modelo_llm, consulta.fallo_llm),
            ('Hay 3 unidades.', 'anthropic', MODELO_RESPUESTA, False),
        )

    def test_stream_con_falla_de_la_api_cierra_el_turno_y_registra(self):
        with self._recuperar(), patch.object(proveedores.anthropic, 'Anthropic') as clase_cliente, \
                self.assertLogs('inventario.asistente.proveedores', level='ERROR'):
            clase_cliente.return_value.messages.stream.side_effect = anthropic.APIConnectionError(
                request=httpx2.Request('POST', URL_API),
            )
            respuesta = self.client.get(reverse('inventario:asistente_stream'), {'pregunta': '¿stock?'})
            cuerpo = b''.join(respuesta.streaming_content).decode()

        self.assertEqual(respuesta.status_code, 200)
        self.assertIn('event: fin', cuerpo)
        self.assertIn('"fallo": true', cuerpo)
        consulta = ConsultaAsistente.objects.get()
        self.assertEqual((consulta.proveedor_llm, consulta.fallo_llm), ('anthropic', True))

    def test_stream_exitoso_registra_el_modelo(self):
        with self._recuperar(), patch.object(proveedores.anthropic, 'Anthropic') as clase_cliente:
            clase_cliente.return_value.messages.stream.return_value = _stream(['Hay ', '3 unidades.'])
            respuesta = self.client.get(reverse('inventario:asistente_stream'), {'pregunta': '¿stock?'})
            b''.join(respuesta.streaming_content)

        consulta = ConsultaAsistente.objects.get()
        self.assertEqual(
            (consulta.respuesta, consulta.proveedor_llm, consulta.modelo_llm, consulta.fallo_llm),
            ('Hay 3 unidades.', 'anthropic', MODELO_RESPUESTA, False),
        )
