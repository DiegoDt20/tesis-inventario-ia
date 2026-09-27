"""Tests del motor de decisiones de reposición (inventario/decisiones).

Lógica determinística: no hay modelos de ML involucrados en estas pruebas
más allá de un ModeloEntrenado "de utilería" que exige la FK de Prediccion.
"""
from datetime import datetime, timedelta
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from inventario.decisiones.calculos import cantidad_a_pedir, punto_reorden, stock_seguridad
from inventario.decisiones.motor import calcular_desviaciones_demanda, calcular_recomendacion
from inventario.models import ModeloEntrenado, Pedido, PedidoDetalle, Prediccion, Producto, Recomendacion


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

    def test_con_pronostico_guarda_metodo_y_fecha_de_corte(self):
        producto = self._crear_producto('PIN-CORTE', stock_actual=0)
        self._crear_predicciones(producto, [2] * 30)

        datos = calcular_recomendacion(producto, nivel_servicio_objetivo=0.95, dias_cobertura=30)

        self.assertEqual(datos['metodo'], Recomendacion.Metodo.PRONOSTICO)
        # El pronóstico empieza mañana: el histórico llega hasta hoy.
        self.assertEqual(datos['fecha_corte_historico'], timezone.localdate())
        self.assertIsNone(datos['demanda_diaria_historica'])
        self.assertIn('Método: pronóstico', datos['explicacion'])


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


class PuntoReordenSinPronosticoTests(TestCase):
    """Productos sin pronóstico en el lote vigente (su categoría no llega a
    los días con venta que exige el modelo): no desaparecen, se recomiendan
    solo con punto de reorden y stock mínimo."""

    def setUp(self):
        self.modelo = ModeloEntrenado.objects.create(
            fase=ModeloEntrenado.Fase.BASE, algoritmo='XGBRegressor', hiperparametros={},
            mae=1.0, rmse=1.0, smape=0.1, r2=0.9, ruta_archivo='artefactos/modelos_ml/test.json',
        )

    def _producto(self, codigo, stock_actual, stock_minimo=0, categoria='solvente'):
        return Producto.objects.create(
            codigo=codigo, nombre='Thinner', precio_venta='20.00', costo_compra='12.00',
            stock_actual=stock_actual, stock_minimo=stock_minimo, lead_time_dias=4, categoria=categoria,
        )

    def _pedido(self, producto, dia, cantidad):
        pedido = Pedido.objects.create(
            fecha_solicitud=timezone.make_aware(datetime(2026, 8, dia)), cliente='C-1',
            canal=Pedido.Canal.MOSTRADOR, origen='real',
        )
        PedidoDetalle.objects.create(pedido=pedido, producto=producto, cantidad_solicitada=cantidad)

    def _predicciones(self, producto, fecha_generacion):
        for i in range(1, 11):
            Prediccion.objects.create(
                producto=producto, fecha_generacion=fecha_generacion,
                fecha_objetivo=datetime(2026, 8, 31).date() + timedelta(days=i),
                demanda_predicha=5, modelo=self.modelo,
            )

    def test_sin_predicciones_usa_punto_de_reorden_con_demanda_historica(self):
        producto = self._producto('SOL-1', stock_actual=1)
        # Periodo real del 1 al 10 de agosto: 10 u. en total -> 1 u./día.
        self._pedido(producto, 1, 5)
        self._pedido(producto, 10, 5)

        datos = calcular_recomendacion(producto, nivel_servicio_objetivo=0.95, dias_cobertura=30)

        self.assertEqual(datos['metodo'], Recomendacion.Metodo.PUNTO_REORDEN)
        self.assertIsNone(datos['demanda_predicha_periodo'])
        self.assertAlmostEqual(datos['demanda_diaria_historica'], 1.0)
        self.assertEqual(datos['dias_horizonte'], 30)
        self.assertEqual(datos['fecha_corte_historico'], datetime(2026, 8, 10).date())
        rop_esperado = 1.0 * 4 + datos['stock_seguridad']
        self.assertAlmostEqual(datos['punto_reorden'], rop_esperado)
        # Order-up-to con la demanda histórica: 30 u. + SS - 1 en stock.
        self.assertAlmostEqual(datos['cantidad_sugerida'], 30 + datos['stock_seguridad'] - 1)
        self.assertEqual(datos['estado'], Recomendacion.Estado.CRITICO)
        self.assertIn('punto de reorden', datos['explicacion'])
        self.assertIn('10/08/2026', datos['explicacion'])

    def test_stock_minimo_es_el_piso_del_punto_de_reorden(self):
        # Sin ventas registradas: demanda 0, SS 0; solo manda el stock mínimo.
        producto = self._producto('SOL-2', stock_actual=3, stock_minimo=8)

        datos = calcular_recomendacion(producto, nivel_servicio_objetivo=0.95, dias_cobertura=30)

        self.assertEqual(datos['metodo'], Recomendacion.Metodo.PUNTO_REORDEN)
        self.assertEqual(datos['punto_reorden'], 8.0)
        self.assertEqual(datos['stock_minimo_snapshot'], 8)
        self.assertEqual(datos['estado'], Recomendacion.Estado.REPONER)
        self.assertEqual(datos['cantidad_sugerida'], 5.0)  # hasta el stock mínimo

    def test_predicciones_de_un_lote_anterior_no_cuentan(self):
        # El producto tuvo pronóstico en una corrida vieja, pero el lote
        # vigente (otro producto, más reciente) ya no lo incluye.
        producto = self._producto('SOL-3', stock_actual=10)
        otro = self._producto('LAT-1', stock_actual=10, categoria='latex')
        ahora = timezone.now()
        self._predicciones(producto, ahora - timedelta(days=2))
        self._predicciones(otro, ahora)

        datos = calcular_recomendacion(producto, nivel_servicio_objetivo=0.95, dias_cobertura=30)
        datos_otro = calcular_recomendacion(otro, nivel_servicio_objetivo=0.95, dias_cobertura=30)

        self.assertEqual(datos['metodo'], Recomendacion.Metodo.PUNTO_REORDEN)
        self.assertEqual(datos_otro['metodo'], Recomendacion.Metodo.PRONOSTICO)
        self.assertEqual(datos_otro['fecha_corte_historico'], datetime(2026, 8, 31).date())

    def test_comando_no_omite_productos_sin_pronostico(self):
        con_pronostico = self._producto('LAT-2', stock_actual=10, categoria='latex')
        self._producto('SOL-4', stock_actual=10)
        self._predicciones(con_pronostico, timezone.now())

        call_command('generar_recomendaciones', stdout=StringIO())

        metodos = dict(Recomendacion.objects.values_list('producto__codigo', 'metodo'))
        self.assertEqual(metodos, {
            'LAT-2': Recomendacion.Metodo.PRONOSTICO,
            'SOL-4': Recomendacion.Metodo.PUNTO_REORDEN,
        })

    def test_propiedades_para_la_tarjeta(self):
        producto = self._producto('SOL-5', stock_actual=0, stock_minimo=10)
        r = Recomendacion(
            producto=producto, metodo=Recomendacion.Metodo.PUNTO_REORDEN, demanda_predicha_periodo=None,
            demanda_diaria_historica=0.5, dias_horizonte=30, lead_time_usado=4.0, stock_seguridad=1.0,
            punto_reorden=10.0, cantidad_sugerida=16.0,
        )
        self.assertEqual(r.demanda_diaria, 0.5)
        self.assertAlmostEqual(r.punto_reorden_calculado, 3.0)
        self.assertAlmostEqual(r.demanda_historica_periodo, 15.0)
        self.assertEqual(r.dias_cobertura_compra, 32)  # 16 u. / 0.5 u. por día


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


class DesviacionDemandaTests(TestCase):
    """La desviación que alimenta el stock de seguridad: solo pedidos del
    origen pedido y sobre todos los días del periodo de ese origen."""

    def setUp(self):
        self.producto = Producto.objects.create(codigo='PIN-SD1', nombre='P', precio_venta=10, costo_compra=5)
        self.otro = Producto.objects.create(codigo='PIN-SD2', nombre='Q', precio_venta=10, costo_compra=5)

    def _pedido(self, producto, dia, mes, cantidad, origen):
        pedido = Pedido.objects.create(
            fecha_solicitud=timezone.make_aware(datetime(2026, mes, dia)), cliente='C-1',
            canal=Pedido.Canal.MOSTRADOR, origen=origen,
        )
        PedidoDetalle.objects.create(pedido=pedido, producto=producto, cantidad_solicitada=cantidad)

    def test_no_mezcla_pedidos_de_prueba(self):
        # Real: agosto (1 al 10), pedidos de 2 u. los días 1 y 10.
        self._pedido(self.producto, 1, 8, 2, 'real')
        self._pedido(self.producto, 10, 8, 2, 'real')
        # Prueba: un pedido enorme en septiembre que dispararía la desviación.
        self._pedido(self.producto, 15, 9, 500, 'prueba')

        sigma_real = calcular_desviaciones_demanda('real')[self.producto.pk]

        self.assertLess(sigma_real, 1.5)

    def test_un_solo_pedido_no_da_desviacion_cero(self):
        # El periodo real va del 1 al 10 de agosto (lo marca el otro
        # producto). El producto con un único pedido de 10 u. tiene 9 días en
        # cero: su desviación no puede ser 0 (antes lo era, porque su serie
        # se rellenaba solo entre su primer y su último pedido).
        self._pedido(self.otro, 1, 8, 1, 'real')
        self._pedido(self.otro, 10, 8, 1, 'real')
        self._pedido(self.producto, 5, 8, 10, 'real')

        sigma = calcular_desviaciones_demanda('real')[self.producto.pk]

        # 10 días: [0,0,0,0,10,0,0,0,0,0] -> desviación muestral = sqrt(10).
        self.assertAlmostEqual(sigma, 10 ** 0.5)

    def test_producto_sin_pedidos_del_origen_no_aparece(self):
        self._pedido(self.producto, 5, 9, 10, 'prueba')
        self._pedido(self.otro, 5, 8, 1, 'real')
        self.assertNotIn(self.producto.pk, calcular_desviaciones_demanda('real'))
