"""Tests del motor de decisiones de reposición (inventario/decisiones).

Lógica determinística: no hay modelos de ML involucrados en estas pruebas
más allá de un ModeloEntrenado "de utilería" que exige la FK de Prediccion.
"""
from datetime import timedelta
from decimal import Decimal

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from inventario.decisiones.calculos import cantidad_a_pedir, punto_reorden, stock_seguridad
from inventario.decisiones.motor import calcular_recomendacion
from inventario.models import ModeloEntrenado, Prediccion, Producto, Recomendacion


class CalculosTests(SimpleTestCase):
    def test_stock_seguridad_crece_con_el_nivel_de_servicio(self):
        ss_90 = stock_seguridad(desviacion_demanda=5, lead_time=7, nivel_servicio_objetivo=0.90)
        ss_99 = stock_seguridad(desviacion_demanda=5, lead_time=7, nivel_servicio_objetivo=0.99)
        self.assertGreater(ss_99, ss_90)

    def test_stock_seguridad_es_cero_sin_variabilidad(self):
        # Sin desviación no hace falta colchón, sin importar el lead time.
        self.assertEqual(stock_seguridad(0, lead_time=10, nivel_servicio_objetivo=0.99), 0)

    def test_punto_reorden_crece_con_el_lead_time(self):
        rop_corto = punto_reorden(demanda_diaria_esperada=3, lead_time=5, stock_seguridad=4)
        rop_largo = punto_reorden(demanda_diaria_esperada=3, lead_time=15, stock_seguridad=4)
        self.assertGreater(rop_largo, rop_corto)

    def test_cantidad_a_pedir_nunca_es_negativa(self):
        cantidad = cantidad_a_pedir(
            demanda_periodo=10, stock_actual=1000, stock_seguridad=5, pedidos_en_transito=0,
        )
        self.assertEqual(cantidad, 0.0)

    def test_cantidad_a_pedir_descuenta_lo_que_ya_hay_y_lo_en_transito(self):
        sin_transito = cantidad_a_pedir(100, stock_actual=10, stock_seguridad=5, pedidos_en_transito=0)
        con_transito = cantidad_a_pedir(100, stock_actual=10, stock_seguridad=5, pedidos_en_transito=20)
        self.assertLess(con_transito, sin_transito)
        self.assertGreaterEqual(con_transito, 0.0)


class MotorRecomendacionesTests(TestCase):
    def setUp(self):
        self.modelo = ModeloEntrenado.objects.create(
            fase=ModeloEntrenado.Fase.BASE,
            algoritmo='XGBRegressor',
            hiperparametros={},
            mae=1.0, rmse=1.0, smape=0.1, r2=0.9,
            ruta_archivo='artefactos/modelos_ml/test.json',
        )

    def _crear_producto(self, codigo, stock_actual, lead_time_dias=5):
        return Producto.objects.create(
            codigo=codigo,
            nombre='Pintura de prueba',
            precio_venta='50.00',
            costo_compra='30.00',
            stock_actual=stock_actual,
            lead_time_dias=lead_time_dias,
        )

    def _crear_predicciones(self, producto, valores):
        fecha_generacion = timezone.now()
        hoy = timezone.localdate()
        for i, valor in enumerate(valores, start=1):
            Prediccion.objects.create(
                producto=producto,
                fecha_generacion=fecha_generacion,
                fecha_objetivo=hoy + timedelta(days=i),
                demanda_predicha=valor,
                modelo=self.modelo,
            )

    def test_producto_sin_stock_es_critico(self):
        producto = self._crear_producto('PIN-CRIT', stock_actual=0)
        self._crear_predicciones(producto, [5] * 30)

        datos = calcular_recomendacion(producto, nivel_servicio_objetivo=0.95, dias_cobertura=30)

        self.assertIsNotNone(datos)
        self.assertEqual(datos['estado'], Recomendacion.Estado.CRITICO)
        self.assertGreater(datos['cantidad_sugerida'], 0)

    def test_producto_con_stock_alto_es_exceso(self):
        producto = self._crear_producto('PIN-EXCESO', stock_actual=100_000, lead_time_dias=5)
        self._crear_predicciones(producto, [1] * 30)

        datos = calcular_recomendacion(producto, nivel_servicio_objetivo=0.95, dias_cobertura=30)

        self.assertIsNotNone(datos)
        self.assertEqual(datos['estado'], Recomendacion.Estado.EXCESO)
        self.assertEqual(datos['cantidad_sugerida'], 0.0)

    def test_sin_predicciones_no_genera_recomendacion(self):
        producto = self._crear_producto('PIN-SINPRED', stock_actual=10)
        datos = calcular_recomendacion(producto, nivel_servicio_objetivo=0.95, dias_cobertura=30)
        self.assertIsNone(datos)

    def test_explicacion_incluye_los_numeros_clave(self):
        producto = self._crear_producto('PIN-EXPLICA', stock_actual=0, lead_time_dias=5)
        self._crear_predicciones(producto, [3] * 30)

        datos = calcular_recomendacion(producto, nivel_servicio_objetivo=0.95, dias_cobertura=30)

        self.assertIn(f"{datos['cantidad_sugerida']:.0f}", datos['explicacion'])
        self.assertIn('95%', datos['explicacion'])
        self.assertIn('configurado en la ficha del producto', datos['explicacion'])

    def test_guarda_demanda_de_categoria_y_participacion(self):
        # Predicción por categoría repartida 25% / 75% entre dos productos.
        producto = self._crear_producto('PIN-CAT1', stock_actual=0)
        otro = self._crear_producto('PIN-CAT2', stock_actual=0)
        Producto.objects.filter(pk__in=[producto.pk, otro.pk]).update(categoria='latex')
        producto.refresh_from_db()
        fecha_generacion = timezone.now()
        hoy = timezone.localdate()
        for i in range(1, 11):
            for p, participacion in ((producto, 0.25), (otro, 0.75)):
                Prediccion.objects.create(
                    producto=p, fecha_generacion=fecha_generacion, fecha_objetivo=hoy + timedelta(days=i),
                    demanda_predicha=8 * participacion, modelo=self.modelo,
                    nivel_prediccion='categoria', participacion_usada=participacion,
                )

        datos = calcular_recomendacion(producto, nivel_servicio_objetivo=0.95, dias_cobertura=30)

        self.assertEqual(datos['dias_horizonte'], 10)
        self.assertAlmostEqual(datos['demanda_predicha_periodo'], 20.0)
        self.assertAlmostEqual(datos['demanda_categoria_periodo'], 80.0)
        self.assertEqual(datos['participacion_usada'], 0.25)
        self.assertFalse(datos['lead_time_es_real'])
        self.assertEqual(datos['pedidos_en_transito'], 0)

    def test_prediccion_por_producto_no_tiene_datos_de_categoria(self):
        producto = self._crear_producto('PIN-PROD', stock_actual=0)
        self._crear_predicciones(producto, [3] * 30)
        datos = calcular_recomendacion(producto, nivel_servicio_objetivo=0.95, dias_cobertura=30)
        self.assertIsNone(datos['demanda_categoria_periodo'])
        self.assertIsNone(datos['participacion_usada'])


class RecomendacionCostoYCoberturaTests(TestCase):
    def _recomendacion(self, cantidad, costo='30.00', demanda_periodo=60.0, dias=30):
        producto = Producto.objects.create(
            codigo=f'PIN-COSTO-{Producto.objects.count()}', nombre='Pintura',
            precio_venta=Decimal('50.00'), costo_compra=Decimal(costo),
        )
        return Recomendacion(
            producto=producto, cantidad_sugerida=cantidad, demanda_predicha_periodo=demanda_periodo,
            dias_horizonte=dias,
        )

    def test_costo_y_cobertura(self):
        r = self._recomendacion(19.5)  # redondeo comercial: 20 u.
        self.assertEqual(r.unidades_sugeridas, 20)
        self.assertEqual(r.costo_estimado, Decimal('600.00'))
        self.assertEqual(r.dias_cobertura_compra, 10)  # 20 u. / 2 u. por día

    def test_sin_costo_registrado(self):
        self.assertIsNone(self._recomendacion(10, costo='0.00').costo_estimado)

    def test_sin_demanda_no_calcula_cobertura(self):
        self.assertIsNone(self._recomendacion(10, demanda_periodo=0.0).dias_cobertura_compra)
        self.assertIsNone(self._recomendacion(10, dias=None).dias_cobertura_compra)
