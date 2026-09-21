"""Tests del asistente conversacional (RAG): que el recuperador devuelva los
documentos relevantes, que la anonimización elimine los datos sensibles
antes de enviarlos a la API del modelo de lenguaje, que el prompt del
sistema fije el idioma, y que una falla del proveedor de LLM no rompa la
pantalla del chat."""
from datetime import datetime
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from inventario.asistente import recuperador
from inventario.asistente.anonimizador import anonimizar_texto
from inventario.asistente.asistente import PROMPT_SISTEMA, consultar_asistente
from inventario.asistente.proveedores import ErrorProveedorLLM
from inventario.models import ConsultaAsistente, DocumentoIndexado, Pedido, TipoDocumento

DIMENSIONES = 384


def _vector(indice_activo, valor=1.0):
    """Vector de DIMENSIONES ceros con un único componente activo, para
    controlar a mano qué tan "cerca" está un documento de una pregunta por
    distancia coseno, sin depender del modelo real de embeddings (lento y
    no determinístico de instalar en cada corrida de tests)."""
    vector = [0.0] * DIMENSIONES
    vector[indice_activo] = valor
    return vector


class RecuperadorDocumentosRelevantesTests(TestCase):
    def setUp(self):
        self.doc_stock = DocumentoIndexado.objects.create(
            tipo=TipoDocumento.PRODUCTO, referencia_id=1,
            contenido='Producto PIN-001 — Latex Blanco. Stock actual: 40 unidades.',
            embedding=_vector(0),
        )
        self.doc_indicador = DocumentoIndexado.objects.create(
            tipo=TipoDocumento.INDICADOR, referencia_id=None,
            contenido='Nivel de servicio (NS): 92.0%.',
            embedding=_vector(1),
        )

    def test_recupera_el_documento_mas_cercano_a_la_pregunta(self):
        with patch.object(recuperador, 'generar_embedding', return_value=_vector(0)):
            resultados = recuperador.recuperar_documentos('¿cuánto stock hay?', n=1)

        self.assertEqual(resultados, [self.doc_stock])

    def test_recupera_los_n_documentos_pedidos_ordenados_por_cercania(self):
        with patch.object(recuperador, 'generar_embedding', return_value=_vector(1)):
            resultados = recuperador.recuperar_documentos('¿cómo va el nivel de servicio?', n=2)

        self.assertEqual(resultados[0], self.doc_indicador)
        self.assertEqual(len(resultados), 2)


class AnonimizacionTests(TestCase):
    def test_redacta_nombre_de_cliente_registrado(self):
        Pedido.objects.create(
            fecha_solicitud=timezone.make_aware(datetime(2026, 8, 1)),
            cliente='Constructora Los Andes S.A.C.',
            canal=Pedido.Canal.MOSTRADOR,
        )
        texto = 'El pedido de Constructora Los Andes S.A.C. incluye 10 unidades de PIN-001.'

        resultado = anonimizar_texto(texto)

        self.assertNotIn('Constructora Los Andes S.A.C.', resultado)
        self.assertIn('[CLIENTE]', resultado)
        self.assertIn('PIN-001', resultado)
        self.assertIn('10 unidades', resultado)

    def test_redacta_correo_y_telefono(self):
        texto = 'Contacto: ventas@empresa.com, teléfono 987654321.'

        resultado = anonimizar_texto(texto)

        self.assertNotIn('ventas@empresa.com', resultado)
        self.assertNotIn('987654321', resultado)

    def test_no_toca_fechas_ni_codigos_de_producto_ni_cantidades(self):
        texto = 'PIN-001 vencía el 24/08/2026, periodo 2026-08, quedan 40 unidades.'

        resultado = anonimizar_texto(texto)

        self.assertEqual(resultado, texto)

    def test_redacta_razon_social_configurada(self):
        with self.settings(EMPRESA_RAZON_SOCIAL='Pinturas del Sur E.I.R.L.'):
            resultado = anonimizar_texto('Comprobante emitido por Pinturas del Sur E.I.R.L.')

        self.assertNotIn('Pinturas del Sur E.I.R.L.', resultado)
        self.assertIn('[EMPRESA]', resultado)


class ConsultarAsistenteAnonimizaContextoTests(TestCase):
    """El contexto que se arma para el proveedor de LLM nunca debe llevar el
    nombre del cliente, aunque venga en un documento indexado."""

    def test_contexto_enviado_al_proveedor_esta_anonimizado(self):
        Pedido.objects.create(
            fecha_solicitud=timezone.make_aware(datetime(2026, 8, 1)),
            cliente='Juan Pérez',
            canal=Pedido.Canal.MOSTRADOR,
        )
        documento = DocumentoIndexado.objects.create(
            tipo=TipoDocumento.PRODUCTO, referencia_id=1,
            contenido='Pedido reciente de Juan Pérez: 5 unidades de PIN-002.',
            embedding=_vector(0),
        )

        capturado = {}

        class ProveedorFalso:
            def generar_respuesta(self, mensajes):
                capturado['mensajes'] = mensajes
                return 'Respuesta de prueba.'

        with patch('inventario.asistente.asistente.recuperar_documentos', return_value=[documento]), \
                patch('inventario.asistente.asistente.obtener_proveedor', return_value=ProveedorFalso()):
            consultar_asistente('¿qué pidió Juan Pérez?')

        contenido_enviado = capturado['mensajes'][1]['content']
        self.assertNotIn('Juan Pérez', contenido_enviado)
        self.assertIn('[CLIENTE]', contenido_enviado)


class FalloProveedorLLMTests(TestCase):
    def setUp(self):
        self.documento = DocumentoIndexado.objects.create(
            tipo=TipoDocumento.INDICADOR, referencia_id=None,
            contenido='Nivel de servicio (NS): 80.0%.',
            embedding=_vector(0),
        )
        self.usuario = User.objects.create_user(username='u1', password='pass12345')
        self.client.login(username='u1', password='pass12345')

    def test_consultar_asistente_no_lanza_si_el_proveedor_falla(self):
        with patch('inventario.asistente.asistente.recuperar_documentos', return_value=[self.documento]), \
                patch('inventario.asistente.asistente.obtener_proveedor') as mock_obtener:
            mock_obtener.return_value.generar_respuesta.side_effect = ErrorProveedorLLM('sin conexión')

            resultado = consultar_asistente('¿cuál es el nivel de servicio?')

        self.assertTrue(resultado.fallo)
        self.assertIn('Nivel de servicio (NS): 80.0%.', resultado.respuesta)

    def test_pantalla_del_chat_no_se_rompe_si_el_proveedor_falla(self):
        with patch('inventario.asistente.asistente.recuperar_documentos', return_value=[self.documento]), \
                patch('inventario.asistente.asistente.obtener_proveedor') as mock_obtener:
            mock_obtener.return_value.generar_respuesta.side_effect = ErrorProveedorLLM('sin conexión')

            respuesta = self.client.post(
                reverse('inventario:asistente_chat'), {'pregunta': '¿cuál es el nivel de servicio?'},
                follow=True,
            )

        self.assertEqual(respuesta.status_code, 200)
        consulta = ConsultaAsistente.objects.get()
        self.assertTrue(consulta.respuesta)
        self.assertIn('Nivel de servicio (NS): 80.0%.', consulta.respuesta)


class PromptSistemaIdiomaTests(TestCase):
    """qwen2.5 (el modelo local configurado) mezcla otros idiomas a mitad de
    la respuesta si no se le fija el español de entrada: el prompt del
    sistema debe empezar con esa instrucción, antes de cualquier otra."""

    def test_prompt_sistema_fija_el_idioma_espanol_al_inicio(self):
        primera_oracion = PROMPT_SISTEMA.strip().split('.')[0].lower()

        self.assertIn('español', primera_oracion)
        self.assertIn('siempre', primera_oracion)

    def test_prompt_sistema_mantiene_las_reglas_de_no_calcular(self):
        self.assertIn('CONTEXTO', PROMPT_SISTEMA)
        self.assertIn('no calcules', PROMPT_SISTEMA.lower())
