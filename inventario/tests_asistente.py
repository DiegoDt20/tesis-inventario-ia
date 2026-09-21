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

from inventario.asistente import indexador, recuperador
from inventario.asistente.anonimizador import anonimizar_texto
from inventario.asistente.asistente import PROMPT_SISTEMA, consultar_asistente
from inventario.asistente.proveedores import ErrorProveedorLLM
from inventario.models import (
    Categoria,
    ConsultaAsistente,
    DocumentoIndexado,
    Pedido,
    Producto,
    Recomendacion,
    TipoDocumento,
)

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


class RecuperadorPreguntaGeneralPriorizaResumenesTests(TestCase):
    """Una pregunta general sobre el negocio debe traer los documentos
    agregados (indicadores, resúmenes), no fichas de producto sueltas,
    aunque una ficha de producto sea la más cercana por similitud pura."""

    def setUp(self):
        self.doc_producto = DocumentoIndexado.objects.create(
            tipo=TipoDocumento.PRODUCTO, referencia_id=1,
            contenido='Producto PIN-001 — Latex Blanco. Stock actual: 40 unidades.',
            embedding=_vector(0),
        )
        self.doc_resumen_categoria = DocumentoIndexado.objects.create(
            tipo=TipoDocumento.RESUMEN_CATEGORIA, referencia_id=None,
            contenido='Resumen de la categoría Látex: 50 producto(s) activo(s), stock total de 900 unidades.',
            embedding=_vector(1),
        )
        self.doc_indicador = DocumentoIndexado.objects.create(
            tipo=TipoDocumento.INDICADOR, referencia_id=None,
            contenido='Indicadores del periodo: EI 90%, NS 85%.',
            embedding=_vector(2),
        )

    def test_pregunta_general_prioriza_resumenes_sobre_ficha_de_producto(self):
        # La pregunta coincide EXACTO con la ficha de producto (sería la
        # más cercana sin priorización) y nada con los resúmenes.
        with patch.object(recuperador, 'generar_embedding', return_value=_vector(0)):
            resultados = recuperador.recuperar_documentos('¿cómo va el negocio?', n=2)

        self.assertEqual(set(resultados), {self.doc_resumen_categoria, self.doc_indicador})
        self.assertNotIn(self.doc_producto, resultados)

    def test_pregunta_puntual_no_activa_la_priorizacion(self):
        with patch.object(recuperador, 'generar_embedding', return_value=_vector(0)):
            resultados = recuperador.recuperar_documentos('¿cuánto stock tiene PIN-001?', n=1)

        self.assertEqual(resultados, [self.doc_producto])

    def test_recomendacion_critica_del_ultimo_lote_tambien_se_prioriza(self):
        producto = Producto.objects.create(
            codigo='PIN-900', nombre='Prod crítico',
            precio_venta='10.00', costo_compra='5.00', stock_actual=1,
        )
        recomendacion_critica = Recomendacion.objects.create(
            producto=producto, fecha_generacion=timezone.now(), estado=Recomendacion.Estado.CRITICO,
            stock_actual_snapshot=1, demanda_predicha_periodo=10, desviacion_demanda=1,
            lead_time_usado=7, stock_seguridad=5, punto_reorden=8, cantidad_sugerida=15,
            nivel_servicio_objetivo=0.95, explicacion='Stock crítico, pedir de inmediato.',
        )
        doc_recomendacion = DocumentoIndexado.objects.create(
            tipo=TipoDocumento.RECOMENDACION, referencia_id=recomendacion_critica.pk,
            contenido='Recomendación vigente para PIN-900 (Crítico): pedir de inmediato.',
            embedding=_vector(3),
        )

        with patch.object(recuperador, 'generar_embedding', return_value=_vector(0)):
            resultados = recuperador.recuperar_documentos('resumen del negocio', n=3)

        self.assertIn(doc_recomendacion, resultados)
        self.assertNotIn(self.doc_producto, resultados)


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


class IndexadorDocumentosResumenTests(TestCase):
    """Documentos agregados que evitan que una pregunta general se responda
    con fichas de producto sueltas (ver RecuperadorPreguntaGeneralPriorizaResumenesTests)."""

    def test_resumen_categoria_agrega_conteo_y_stock_ignorando_inactivos(self):
        Producto.objects.create(
            codigo='PIN-010', nombre='Latex A', categoria=Categoria.LATEX,
            precio_venta='10.00', costo_compra='5.00', stock_actual=30, activo=True,
        )
        Producto.objects.create(
            codigo='PIN-011', nombre='Latex B', categoria=Categoria.LATEX,
            precio_venta='10.00', costo_compra='5.00', stock_actual=20, activo=True,
        )
        Producto.objects.create(
            codigo='PIN-012', nombre='Latex inactivo', categoria=Categoria.LATEX,
            precio_venta='10.00', costo_compra='5.00', stock_actual=999, activo=False,
        )

        documentos = indexador._documentos_resumen_categoria()

        contenidos = [contenido for _, _, contenido in documentos]
        self.assertTrue(any('Látex' in c and '2 producto' in c and '50 unidades' in c for c in contenidos))
        self.assertFalse(any('999' in c for c in contenidos))
        self.assertTrue(all(tipo == TipoDocumento.RESUMEN_CATEGORIA for tipo, _, _ in documentos))

    def test_resumen_estado_cuenta_por_estado_del_ultimo_lote(self):
        producto = Producto.objects.create(
            codigo='PIN-020', nombre='Prod', precio_venta='10.00', costo_compra='5.00', stock_actual=5,
        )
        Recomendacion.objects.create(
            producto=producto, fecha_generacion=timezone.now(), estado=Recomendacion.Estado.CRITICO,
            stock_actual_snapshot=5, demanda_predicha_periodo=10, desviacion_demanda=1,
            lead_time_usado=7, stock_seguridad=2, punto_reorden=5, cantidad_sugerida=10,
            nivel_servicio_objetivo=0.95, explicacion='x',
        )

        documentos = indexador._documento_resumen_estado()

        self.assertEqual(len(documentos), 1)
        tipo, referencia_id, contenido = documentos[0]
        self.assertEqual(tipo, TipoDocumento.RESUMEN_ESTADO)
        self.assertIn('1 en estado crítico', contenido)

    def test_resumen_estado_vacio_sin_lote_de_recomendaciones(self):
        self.assertEqual(indexador._documento_resumen_estado(), [])

    def test_resumen_anomalias_dice_que_no_hay_si_esta_vacio(self):
        documentos = indexador._documento_resumen_anomalias()

        self.assertEqual(len(documentos), 1)
        self.assertIn('no hay anomalías', documentos[0][2])
