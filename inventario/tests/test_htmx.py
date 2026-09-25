"""Tests de la capa de interactividad HTMX: que las vistas devuelvan el
fragmento parcial correcto cuando la petición trae el encabezado
"HX-Request" (filtros y paginación sin recarga, aceptar/rechazar
recomendación, marcar anomalía revisada, validación en vivo de los
formularios de pedidos y movimientos) y la página completa cuando no.

La lógica de negocio en sí (qué hace aceptar/rechazar, qué es una salida
válida, etc.) ya está cubierta en test_vistas.py y test_decisiones.py; aquí
solo se verifica el contrato específico de HTMX: qué plantilla se devuelve,
que el fragmento no incluya el "chrome" de la página completa, y que el
estado en base de datos cambie igual que con una petición normal."""
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ..asistente.proveedores import ErrorProveedorLLM
from ..forms import etiqueta_producto
from ..models import (
    Anomalia,
    ConsultaAsistente,
    DocumentoIndexado,
    Movimiento,
    Pedido,
    PedidoDetalle,
    Producto,
    Recomendacion,
    TipoDocumento,
)

DIMENSIONES = 384


def _vector(indice_activo=0):
    vector = [0.0] * DIMENSIONES
    vector[indice_activo] = 1.0
    return vector


def _crear_operador(username='operador1'):
    call_command('crear_grupos_permisos')
    usuario = User.objects.create_user(username=username, password='pass12345')
    usuario.groups.add(Group.objects.get(name='operador'))
    return usuario


def _crear_administrador(username='admin1'):
    call_command('crear_grupos_permisos')
    usuario = User.objects.create_user(username=username, password='pass12345', is_staff=True)
    usuario.groups.add(Group.objects.get(name='administrador'))
    return usuario


class DashboardPartialTests(TestCase):
    def setUp(self):
        self.usuario = _crear_operador()
        self.client.login(username='operador1', password='pass12345')

    def test_peticion_normal_devuelve_la_pagina_completa(self):
        respuesta = self.client.get(reverse('inventario:dashboard'))

        self.assertContains(respuesta, 'sidebar-nav')
        self.assertContains(respuesta, '<html')

    def test_peticion_htmx_devuelve_solo_la_zona_de_resultados(self):
        respuesta = self.client.get(reverse('inventario:dashboard'), HTTP_HX_REQUEST='true')

        self.assertNotContains(respuesta, 'sidebar-nav')
        self.assertNotContains(respuesta, '<html')
        # El contenido en sí (paneles del dashboard) sigue estando.
        self.assertContains(respuesta, 'Exactitud del inventario')

    def test_peticion_htmx_respeta_el_filtro_de_origen(self):
        respuesta = self.client.get(
            reverse('inventario:dashboard'), {'origen': 'real'}, HTTP_HX_REQUEST='true',
        )
        self.assertEqual(respuesta.status_code, 200)


class PedidoListaPartialTests(TestCase):
    def setUp(self):
        self.usuario = _crear_operador()
        self.client.login(username='operador1', password='pass12345')
        self.producto = Producto.objects.create(
            codigo='PIN-800', nombre='Latex Verde', precio_venta='50.00', costo_compra='30.00',
        )
        self.pedido = Pedido.objects.create(
            fecha_solicitud=timezone.now(), cliente='Cliente A',
            canal=Pedido.Canal.MOSTRADOR, estado=Pedido.Estado.ATENDIDO, origen='real',
        )
        PedidoDetalle.objects.create(
            pedido=self.pedido, producto=self.producto,
            cantidad_solicitada=5, cantidad_atendida=5,
        )

    def test_peticion_normal_incluye_el_chrome_de_la_pagina(self):
        respuesta = self.client.get(reverse('inventario:pedido_lista'))
        self.assertContains(respuesta, 'sidebar-nav')

    def test_peticion_htmx_no_incluye_el_chrome_de_la_pagina(self):
        respuesta = self.client.get(reverse('inventario:pedido_lista'), HTTP_HX_REQUEST='true')
        self.assertNotContains(respuesta, 'sidebar-nav')
        self.assertContains(respuesta, 'Cliente A')

    def test_filtro_por_estado_via_htmx_filtra_igual_que_sin_htmx(self):
        respuesta = self.client.get(
            reverse('inventario:pedido_lista'), {'estado': Pedido.Estado.PENDIENTE},
            HTTP_HX_REQUEST='true',
        )
        self.assertNotContains(respuesta, 'Cliente A')


class RecomendacionesPartialYAccionesTests(TestCase):
    def setUp(self):
        self.usuario = _crear_administrador()
        self.client.login(username='admin1', password='pass12345')
        self.producto = Producto.objects.create(
            codigo='PIN-801', nombre='Esmalte Azul', precio_venta='40.00', costo_compra='25.00',
        )
        self.recomendacion = Recomendacion.objects.create(
            producto=self.producto, fecha_generacion=timezone.now(),
            estado=Recomendacion.Estado.CRITICO, stock_actual_snapshot=0,
            demanda_predicha_periodo=10, desviacion_demanda=1, lead_time_usado=5,
            stock_seguridad=2, punto_reorden=5, cantidad_sugerida=15,
            nivel_servicio_objetivo=0.95, explicacion='Reponer urgente.',
        )

    def test_peticion_htmx_no_incluye_el_chrome_de_la_pagina(self):
        respuesta = self.client.get(reverse('inventario:recomendaciones_lista'), HTTP_HX_REQUEST='true')
        self.assertNotContains(respuesta, 'sidebar-nav')
        self.assertContains(respuesta, 'Esmalte Azul')

    def test_aceptar_por_htmx_devuelve_la_tarjeta_actualizada_sin_redirigir(self):
        respuesta = self.client.post(
            reverse('inventario:recomendacion_decidir', args=[self.recomendacion.pk]),
            {'accion': 'aceptar'}, HTTP_HX_REQUEST='true',
        )

        self.assertEqual(respuesta.status_code, 200)
        self.recomendacion.refresh_from_db()
        self.assertTrue(self.recomendacion.aceptada)
        self.assertIsNotNone(self.recomendacion.fecha_decision)
        # La tarjeta ya no ofrece aceptar/rechazar: el estado quedó decidido.
        self.assertContains(respuesta, 'Aceptada')
        self.assertNotContains(respuesta, 'Rechazar')

    def test_rechazar_por_htmx_actualiza_la_base_de_datos(self):
        respuesta = self.client.post(
            reverse('inventario:recomendacion_decidir', args=[self.recomendacion.pk]),
            {'accion': 'rechazar'}, HTTP_HX_REQUEST='true',
        )

        self.assertEqual(respuesta.status_code, 200)
        self.recomendacion.refresh_from_db()
        self.assertFalse(self.recomendacion.aceptada)
        self.assertContains(respuesta, 'Rechazada')

    def test_decidir_sin_htmx_sigue_redirigiendo_como_antes(self):
        respuesta = self.client.post(
            reverse('inventario:recomendacion_decidir', args=[self.recomendacion.pk]),
            {'accion': 'aceptar'},
        )
        self.assertRedirects(respuesta, reverse('inventario:recomendaciones_lista'))


class AnomaliasPartialYAccionesTests(TestCase):
    def setUp(self):
        self.usuario = _crear_administrador()
        self.client.login(username='admin1', password='pass12345')
        self.producto = Producto.objects.create(
            codigo='PIN-802', nombre='Base Blanca', precio_venta='30.00', costo_compra='18.00',
        )
        self.anomalia = Anomalia.objects.create(
            producto=self.producto, fecha_deteccion=timezone.now(),
            tipo=Anomalia.Tipo.DIFERENCIA_INVENTARIO, severidad=Anomalia.Severidad.ALTA,
            score=0.9, valor_observado=10, valor_esperado=5, descripcion='Diferencia grande.',
        )

    def test_peticion_htmx_no_incluye_el_chrome_de_la_pagina(self):
        respuesta = self.client.get(reverse('inventario:anomalias_lista'), HTTP_HX_REQUEST='true')
        self.assertNotContains(respuesta, 'sidebar-nav')
        self.assertContains(respuesta, 'Base Blanca')

    def test_marcar_revisada_por_htmx_repinta_la_lista_y_actualiza_la_bd(self):
        # Se repinta la lista completa (no solo se quita la fila) para que
        # también se actualicen los totales del encabezado de cada conteo.
        respuesta = self.client.post(
            reverse('inventario:anomalia_marcar_revisada', args=[self.anomalia.pk]),
            {'motivo': 'otro'}, HTTP_HX_REQUEST='true', HTTP_HX_PROMPT='Producto mal etiquetado',
        )

        self.assertEqual(respuesta.status_code, 200)
        self.assertNotContains(respuesta, 'sidebar-nav')
        self.assertContains(respuesta, 'No hay anomalías sin revisar')
        self.anomalia.refresh_from_db()
        self.assertTrue(self.anomalia.revisada)
        self.assertIsNotNone(self.anomalia.fecha_revision)
        self.assertEqual(self.anomalia.motivo_revision, 'otro')
        self.assertEqual(self.anomalia.detalle_revision, 'Producto mal etiquetado')

    def test_marcar_revisada_sin_htmx_sigue_redirigiendo_como_antes(self):
        respuesta = self.client.post(
            reverse('inventario:anomalia_marcar_revisada', args=[self.anomalia.pk]),
        )
        self.assertRedirects(respuesta, reverse('inventario:anomalias_lista'))


class MovimientoValidarTests(TestCase):
    """Validación en vivo del formulario de movimientos: avisa si una
    salida dejaría el stock en negativo, antes de intentar guardar."""

    def setUp(self):
        self.usuario = _crear_operador()
        self.client.login(username='operador1', password='pass12345')
        self.producto = Producto.objects.create(
            codigo='PIN-803', nombre='Solvente', precio_venta='20.00', costo_compra='10.00',
            stock_actual=5,
        )
        self.etiqueta = etiqueta_producto(self.producto)

    def test_salida_mayor_al_stock_avisa(self):
        respuesta = self.client.get(reverse('inventario:movimiento_validar'), {
            'producto': self.etiqueta, 'tipo': Movimiento.Tipo.SALIDA, 'cantidad': '100',
        })
        self.assertContains(respuesta, 'aviso-validacion-riesgo')
        self.assertContains(respuesta, 'PIN-803')

    def test_salida_dentro_del_stock_no_avisa(self):
        respuesta = self.client.get(reverse('inventario:movimiento_validar'), {
            'producto': self.etiqueta, 'tipo': Movimiento.Tipo.SALIDA, 'cantidad': '3',
        })
        self.assertNotContains(respuesta, 'aviso-validacion-riesgo')

    def test_ingreso_por_encima_del_stock_no_avisa(self):
        """Un ingreso nunca deja el stock en negativo: la regla solo aplica
        a salidas."""
        respuesta = self.client.get(reverse('inventario:movimiento_validar'), {
            'producto': self.etiqueta, 'tipo': Movimiento.Tipo.INGRESO, 'cantidad': '1000',
        })
        self.assertNotContains(respuesta, 'aviso-validacion-riesgo')

    def test_producto_no_reconocido_no_avisa_ni_falla(self):
        respuesta = self.client.get(reverse('inventario:movimiento_validar'), {
            'producto': 'no existe', 'tipo': Movimiento.Tipo.SALIDA, 'cantidad': '100',
        })
        self.assertEqual(respuesta.status_code, 200)
        self.assertNotContains(respuesta, 'aviso-validacion-riesgo')


class PedidoValidarLineaTests(TestCase):
    """Validación en vivo de una línea de pedido: misma regla que
    evaluar_linea_pedido (forms.py), antes de enviar el formulario."""

    def setUp(self):
        self.usuario = _crear_operador()
        self.client.login(username='operador1', password='pass12345')

    def _validar(self, solicitada, atendida, motivo=''):
        return self.client.get(reverse('inventario:pedido_validar_linea'), {
            'cantidad_solicitada': solicitada, 'cantidad_atendida': atendida,
            'motivo_no_atencion': motivo,
        })

    def test_atendida_mayor_que_solicitada_avisa(self):
        respuesta = self._validar(10, 20)
        self.assertContains(respuesta, 'No puede ser mayor')

    def test_atendida_menor_sin_motivo_avisa(self):
        respuesta = self._validar(10, 5)
        self.assertContains(respuesta, 'indica el motivo')

    def test_atendida_menor_con_motivo_no_avisa(self):
        respuesta = self._validar(10, 5, motivo='sin_stock')
        self.assertNotContains(respuesta, 'aviso-validacion-riesgo')

    def test_atendida_igual_a_solicitada_no_avisa(self):
        respuesta = self._validar(10, 10)
        self.assertNotContains(respuesta, 'aviso-validacion-riesgo')


class AsistenteStreamTests(TestCase):
    """Vista SSE del asistente en tiempo real (proveedores.py:
    generar_respuesta_stream se mockea para no depender de Ollama real)."""

    def setUp(self):
        self.usuario = _crear_operador()
        self.client.login(username='operador1', password='pass12345')
        self.documento = DocumentoIndexado.objects.create(
            tipo=TipoDocumento.INDICADOR, referencia_id=None,
            contenido='EI del periodo: 92.00%.', embedding=_vector(0),
        )

    def _consumir(self, respuesta):
        self.assertTrue(respuesta.streaming)
        self.assertEqual(respuesta['Content-Type'], 'text/event-stream')
        return b''.join(respuesta.streaming_content).decode()

    def test_respuesta_exitosa_transmite_fragmentos_y_evento_final(self):
        with patch('inventario.asistente.asistente.recuperar_documentos', return_value=[self.documento]), \
                patch('inventario.asistente.asistente.obtener_proveedor') as mock_obtener:
            mock_obtener.return_value.generar_respuesta_stream.return_value = iter(['El EI ', 'es 92%.'])

            respuesta = self.client.get(reverse('inventario:asistente_stream'), {'pregunta': '¿Cómo va el EI?'})
            cuerpo = self._consumir(respuesta)

        self.assertIn('event: fragmento', cuerpo)
        self.assertIn('El EI ', cuerpo)
        self.assertIn('es 92%.', cuerpo)
        self.assertIn('event: fin', cuerpo)
        self.assertIn('"fallo": false', cuerpo)
        # Queda auditado en ConsultaAsistente, igual que la vía sin streaming.
        consulta = ConsultaAsistente.objects.get()
        self.assertEqual(consulta.respuesta, 'El EI es 92%.')
        self.assertEqual(consulta.usuario, self.usuario)

    def test_falla_del_proveedor_antes_del_primer_fragmento_no_rompe_el_stream(self):
        with patch('inventario.asistente.asistente.recuperar_documentos', return_value=[self.documento]), \
                patch('inventario.asistente.asistente.obtener_proveedor') as mock_obtener:
            mock_obtener.return_value.generar_respuesta_stream.side_effect = ErrorProveedorLLM('sin conexión')

            respuesta = self.client.get(reverse('inventario:asistente_stream'), {'pregunta': '¿Cómo va el EI?'})
            cuerpo = self._consumir(respuesta)

        self.assertIn('event: fin', cuerpo)
        self.assertIn('"fallo": true', cuerpo)
        self.assertIn('EI del periodo', cuerpo)  # el contexto crudo, sin redactar

    def test_falla_del_proveedor_a_mitad_de_camino_conserva_lo_ya_generado(self):
        def generador_con_falla(_mensajes):
            yield 'Primera parte. '
            raise ErrorProveedorLLM('se perdió la conexión')

        with patch('inventario.asistente.asistente.recuperar_documentos', return_value=[self.documento]), \
                patch('inventario.asistente.asistente.obtener_proveedor') as mock_obtener:
            mock_obtener.return_value.generar_respuesta_stream.side_effect = generador_con_falla

            respuesta = self.client.get(reverse('inventario:asistente_stream'), {'pregunta': '¿Cómo va el EI?'})
            cuerpo = self._consumir(respuesta)

        self.assertIn('Primera parte.', cuerpo)
        self.assertIn('"fallo": true', cuerpo)
        # El aviso de corte va con tildes, que json.dumps escapa como \uXXXX;
        # se verifica sobre la parte sin acentos para no depender de eso.
        self.assertIn('a mitad de la respuesta', cuerpo)

    def test_pregunta_vacia_termina_de_inmediato_sin_consultar_al_proveedor(self):
        with patch('inventario.asistente.asistente.obtener_proveedor') as mock_obtener:
            respuesta = self.client.get(reverse('inventario:asistente_stream'), {'pregunta': '  '})
            cuerpo = self._consumir(respuesta)

        mock_obtener.assert_not_called()
        self.assertIn('event: fin', cuerpo)
        self.assertIn('"fallo": true', cuerpo)
