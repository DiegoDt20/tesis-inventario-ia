"""Tests de la desagregación de predicciones de categoría a producto
(inventario/ml/prediccion.py)."""
from django.test import TestCase
from django.utils import timezone

from inventario.ml.prediccion import calcular_participacion, desagregar_prediccion_categoria
from inventario.models import Categoria, Pedido, PedidoDetalle, Producto


class ParticipacionYDesagregacionTests(TestCase):
    def setUp(self):
        self.producto_a = Producto.objects.create(
            codigo='LAT-A', nombre='Latex A', categoria=Categoria.LATEX,
            precio_venta='10.00', costo_compra='5.00',
        )
        self.producto_b = Producto.objects.create(
            codigo='LAT-B', nombre='Latex B', categoria=Categoria.LATEX,
            precio_venta='10.00', costo_compra='5.00',
        )
        self.sin_historico = Producto.objects.create(
            codigo='LAT-C', nombre='Latex C', categoria=Categoria.LATEX,
            precio_venta='10.00', costo_compra='5.00',
        )

        pedido = Pedido.objects.create(
            fecha_solicitud=timezone.now(), cliente='Cliente de prueba',
            canal=Pedido.Canal.MOSTRADOR, estado=Pedido.Estado.ATENDIDO,
        )
        # A vendió 30 unidades y B 10: participación esperada 0.75 / 0.25.
        # C no tiene ninguna línea de pedido (sin histórico).
        PedidoDetalle.objects.create(pedido=pedido, producto=self.producto_a, cantidad_solicitada=30)
        PedidoDetalle.objects.create(pedido=pedido, producto=self.producto_b, cantidad_solicitada=10)

    def test_producto_sin_historico_recibe_participacion_cero_sin_fallar(self):
        participacion = calcular_participacion()
        fila = participacion[participacion['producto_id'] == self.sin_historico.pk].iloc[0]
        self.assertEqual(fila['participacion'], 0.0)

    def test_las_participaciones_de_una_categoria_suman_uno(self):
        participacion = calcular_participacion()
        total = participacion[participacion['categoria'] == Categoria.LATEX]['participacion'].sum()
        self.assertAlmostEqual(total, 1.0)

    def test_participacion_refleja_las_unidades_vendidas(self):
        participacion = calcular_participacion()
        por_producto = participacion.set_index('producto_id')['participacion']
        self.assertAlmostEqual(por_producto[self.producto_a.pk], 0.75)
        self.assertAlmostEqual(por_producto[self.producto_b.pk], 0.25)
        self.assertAlmostEqual(por_producto[self.sin_historico.pk], 0.0)

    def test_desagregacion_suma_la_prediccion_de_la_categoria(self):
        participacion = calcular_participacion()
        demanda_categoria = 40.0
        reparto = desagregar_prediccion_categoria(Categoria.LATEX, demanda_categoria, participacion)

        self.assertEqual(len(reparto), 3)  # los tres productos de la categoría, incluido el de 0
        suma = sum(valor for _, valor, _ in reparto)
        self.assertAlmostEqual(suma, demanda_categoria)

    def test_desagregacion_no_falla_sin_ninguna_venta_registrada(self):
        PedidoDetalle.objects.all().delete()
        participacion = calcular_participacion()
        reparto = desagregar_prediccion_categoria(Categoria.LATEX, 10.0, participacion)

        self.assertEqual(len(reparto), 3)
        self.assertTrue(all(valor == 0.0 for _, valor, _ in reparto))
