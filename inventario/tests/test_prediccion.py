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


class PronosticoDesdeFinDelHistoricoTests(TestCase):
    """predecir_demanda (nivel categoría) pronostica desde el día siguiente
    al último del histórico aunque una categoría no tenga pedidos ese día."""

    def test_todas_las_categorias_empiezan_al_dia_siguiente_del_corte(self):
        from datetime import date, timedelta
        from io import StringIO
        from unittest import mock

        import numpy as np
        import pandas as pd
        from django.core.management import call_command

        from inventario.management.commands import predecir_demanda
        from inventario.models import ModeloEntrenado, Prediccion, Producto

        for categoria in ('esmalte', 'latex'):
            Producto.objects.create(
                codigo=f'P-{categoria}', nombre=categoria, categoria=categoria,
                precio_venta='10.00', costo_compra='5.00',
            )
        # esmalte vende hasta el 30/08 y látex hasta el 31/08.
        filas = [
            {'fecha': pd.Timestamp(date(2026, 8, 1) + timedelta(days=i)), 'serie_id': categoria, 'cantidad': 3.0}
            for categoria, dias in (('esmalte', 30), ('latex', 31)) for i in range(dias)
        ]
        ModeloEntrenado.objects.create(
            fase=ModeloEntrenado.Fase.AJUSTADO, nivel='categoria', hiperparametros={},
            mae=1, rmse=1, smape=0.1, r2=0.4, ruta_archivo='no-se-usa.json', activo=True,
        )
        modelo = mock.Mock()
        modelo.predict.side_effect = lambda x: np.ones(len(x))
        participacion = pd.DataFrame(
            [{'producto_id': p.pk, 'categoria': p.categoria, 'participacion': 1.0} for p in Producto.objects.all()]
        )
        with mock.patch.object(predecir_demanda, 'leer_demanda_interna_por_categoria', return_value=pd.DataFrame(filas)), \
                mock.patch.object(predecir_demanda, 'calcular_participacion', return_value=participacion), \
                mock.patch.object(predecir_demanda.xgb, 'XGBRegressor', return_value=modelo):
            call_command('predecir_demanda', dias=3, stdout=StringIO())

        primeras = {
            p.producto.categoria: p.fecha_objetivo
            for p in Prediccion.objects.select_related('producto').order_by('-fecha_objetivo')
        }
        self.assertEqual(primeras, {'esmalte': date(2026, 9, 1), 'latex': date(2026, 9, 1)})
