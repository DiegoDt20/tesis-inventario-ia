"""Tests de la exactitud del pronóstico (inventario/ml/evaluacion.py) y del
comando evaluar_predicciones.

Se verifican las definiciones con valores calculados a mano (WAPE,
exactitud, acierto dentro de tolerancia, R² intra-categoría), que las
líneas base usen el mismo test que el modelo sin mirar el futuro, y que el
comando no active ni registre ningún modelo.
"""
import json
import tempfile
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from inventario.management.commands import evaluar_predicciones
from inventario.ml.entrenamiento import HIPERPARAMETROS_BASE, entrenar_modelo, guardar_modelo
from inventario.ml.evaluacion import (
    LINEAS_BASE,
    acierto_tolerancia_relativa,
    calcular_exactitud,
    calcular_wape,
    comparar_exactitud,
    evaluar_exactitud,
    linea_base_media_categoria,
    r2_por_categoria,
)
from inventario.ml.features import construir_features, dividir_temporal
from inventario.models import (
    Categoria,
    ModeloEntrenado,
    Origen,
    Pedido,
    PedidoDetalle,
    Prediccion,
    Producto,
)


def _interno(dias=31, inicio='2026-08-01'):
    """Tres categorías con niveles distintos, como agosto con datos reales."""
    rng = np.random.default_rng(0)
    return pd.concat([
        pd.DataFrame({
            'fecha': pd.date_range(inicio, periods=dias, freq='D'),
            'serie_id': nombre,
            'cantidad': rng.poisson(lam=nivel, size=dias).astype(float),
        })
        for nombre, nivel in [('accesorio', 7), ('esmalte', 65), ('latex', 10)]
    ], ignore_index=True)


class WapeTests(SimpleTestCase):
    def test_valores_conocidos_a_mano(self):
        # |10-8| + |10-14| = 6; Σ real = 20 → WAPE 0.30, exactitud 0.70.
        wape = calcular_wape([10, 10], [8, 14])
        self.assertAlmostEqual(wape, 0.30)
        self.assertAlmostEqual(calcular_exactitud(wape), 0.70)

    def test_sin_demanda_real_devuelve_none_y_queda_fuera_del_total(self):
        self.assertIsNone(calcular_wape([0, 0], [5, 5]))
        resultado = evaluar_exactitud(
            [10, 10, 0, 0], [8, 14, 5, 5], ['A', 'A', 'B', 'B'], tolerancia_relativa=0.2,
        )
        self.assertIsNone(resultado['categorias']['B']['wape'])
        self.assertIsNone(resultado['categorias']['B']['exactitud'])
        self.assertEqual(resultado['total']['categorias_en_wape'], ['A'])
        # Con B dentro sería (6 + 10) / 20 = 0.80.
        self.assertAlmostEqual(resultado['total']['wape'], 0.30)
        # El resto del total sí usa todas las filas.
        self.assertEqual(resultado['total']['dias'], 4)

    def test_exactitud_negativa_sin_truncar(self):
        # Error 8 sobre demanda 4: WAPE 2.0 → exactitud -1.0, no 0.
        resultado = evaluar_exactitud([2, 2], [6, 6], ['A', 'A'], tolerancia_relativa=0.2)
        self.assertAlmostEqual(resultado['categorias']['A']['wape'], 2.0)
        self.assertAlmostEqual(resultado['categorias']['A']['exactitud'], -1.0)
        self.assertAlmostEqual(resultado['total']['exactitud'], -1.0)

    def test_total_es_wape_agregado_y_no_promedio_por_categoria(self):
        # A: 20 / 200 = 0.10; B: 2 / 2 = 1.00. Promedio 0.55; agregado 22 / 202.
        resultado = evaluar_exactitud(
            [100, 100, 1, 1], [90, 110, 2, 2], ['A', 'A', 'B', 'B'], tolerancia_relativa=0.2,
        )
        self.assertAlmostEqual(resultado['categorias']['A']['wape'], 0.10)
        self.assertAlmostEqual(resultado['categorias']['B']['wape'], 1.00)
        self.assertAlmostEqual(resultado['total']['wape'], 22 / 202)
        self.assertNotAlmostEqual(resultado['total']['wape'], 0.55)


class R2PorCategoriaTests(SimpleTestCase):
    def test_r2_intra_distinto_del_global_con_niveles_separados(self):
        # Predecir la media de cada categoría: explica toda la diferencia de
        # nivel (R² global alto) y nada del movimiento interno (R² intra 0).
        real = [95, 105, 98, 102, 9, 11, 8, 12]
        categorias = ['A'] * 4 + ['B'] * 4
        prediccion = [100] * 4 + [10] * 4
        intra = r2_por_categoria(real, prediccion, categorias)
        resultado = evaluar_exactitud(real, prediccion, categorias, tolerancia_relativa=0.2)

        self.assertAlmostEqual(intra['A'], 0.0)
        self.assertAlmostEqual(intra['B'], 0.0)
        self.assertGreater(resultado['total']['r2_global'], 0.99)
        self.assertEqual(resultado['total']['r2_por_categoria'], intra)

    def test_demanda_constante_no_tiene_r2(self):
        self.assertIsNone(r2_por_categoria([5, 5, 5], [4, 5, 6], ['A'] * 3)['A'])


class ToleranciaTests(SimpleTestCase):
    def test_tolerancia_relativa_con_valores_conocidos(self):
        # Errores relativos 0.2 y 0.4; el día con real 0 se mide como |0-1| / 1 = 1.
        self.assertAlmostEqual(acierto_tolerancia_relativa([10, 10], [8, 14], 0.2), 0.5)
        self.assertAlmostEqual(acierto_tolerancia_relativa([10, 10], [8, 14], 0.4), 1.0)
        self.assertAlmostEqual(acierto_tolerancia_relativa([0], [1], 0.5), 0.0)

    def test_se_lee_de_configuracion_y_cambiarla_cambia_el_resultado(self):
        with override_settings(PRONOSTICO_TOLERANCIA_RELATIVA=0.1):
            estricta = evaluar_exactitud([10, 10], [8, 14], ['A', 'A'])
        with override_settings(PRONOSTICO_TOLERANCIA_RELATIVA=0.5):
            holgada = evaluar_exactitud([10, 10], [8, 14], ['A', 'A'])
        self.assertEqual(estricta['tolerancia_relativa'], 0.1)
        self.assertEqual(holgada['tolerancia_relativa'], 0.5)
        self.assertAlmostEqual(estricta['categorias']['A']['acierto_tolerancia'], 0.0)
        self.assertAlmostEqual(holgada['categorias']['A']['acierto_tolerancia'], 1.0)


class LineasBaseTests(SimpleTestCase):
    def setUp(self):
        self.train, self.test = dividir_temporal(construir_features(_interno()), dias_test=7)

    def test_las_tres_se_calculan_sobre_el_mismo_test_que_el_modelo(self):
        for _, funcion in LINEAS_BASE.values():
            prediccion = funcion(self.train, self.test)
            self.assertTrue(prediccion.index.equals(self.test.index))

        resultado = comparar_exactitud(self.train, self.test, self.test['media_movil_7'])
        filas = resultado['predicciones']
        self.assertEqual(len(filas), len(self.test))
        np.testing.assert_array_equal(filas['real'], self.test['cantidad'])
        for clave in ['modelo', *LINEAS_BASE]:
            self.assertEqual(resultado[clave]['total']['dias'], len(self.test))
            self.assertEqual(set(resultado[clave]['categorias']), {'accesorio', 'esmalte', 'latex'})

    def test_media_por_categoria_solo_usa_dias_de_entrenamiento(self):
        esperada = self.train.groupby('serie_id')['cantidad'].mean()
        prediccion = linea_base_media_categoria(self.train, self.test)
        np.testing.assert_allclose(prediccion, self.test['serie_id'].map(esperada))

        alterado = self.test.copy()
        alterado['cantidad'] = 10_000.0
        pd.testing.assert_series_equal(prediccion, linea_base_media_categoria(self.train, alterado))

    def test_media_movil_y_ultimo_valor_no_usan_el_propio_dia(self):
        """Cambiar la demanda del último día de test no cambia ningún
        pronóstico de línea base (todos usan solo días anteriores)."""
        ultimo = self.test['fecha'].max()
        alterado = self.test.copy()
        alterado.loc[alterado['fecha'] == ultimo, 'cantidad'] = 10_000.0
        for _, funcion in LINEAS_BASE.values():
            pd.testing.assert_series_equal(funcion(self.train, self.test), funcion(self.train, alterado))

    def test_media_movil_7_con_valores_conocidos(self):
        df = pd.DataFrame({
            'fecha': pd.date_range('2026-08-01', periods=10, freq='D'),
            'serie_id': 'A',
            'cantidad': [float(i) for i in range(1, 11)],
        })
        train, test = df.iloc[:8], df.iloc[8:]
        # Día 9: media de los días 2..8 = 5; día 10: media de 3..9 = 6.
        np.testing.assert_allclose(LINEAS_BASE['media_movil_7'][1](train, test), [5.0, 6.0])

    def test_mejor_linea_base_y_si_el_modelo_las_supera(self):
        perfecto = comparar_exactitud(self.train, self.test, self.test['cantidad'])
        self.assertTrue(all(perfecto['supera'].values()))
        malo = comparar_exactitud(self.train, self.test, self.test['cantidad'] * 5)
        self.assertFalse(any(malo['supera'].values()))
        wapes = {c: malo[c]['total']['wape'] for c in LINEAS_BASE}
        self.assertEqual(malo['mejor_linea_base'], min(wapes, key=wapes.get))


class ComandoEvaluarPrediccionesTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        interno = _interno()
        self.interno = interno
        with mock.patch('inventario.ml.entrenamiento.DIR_MODELOS', Path(self.tmp.name)):
            train, _ = dividir_temporal(construir_features(interno), dias_test=7)
            modelo = entrenar_modelo(train, {**HIPERPARAMETROS_BASE, 'n_estimators': 20})
            ruta = guardar_modelo(modelo, 'ajustado.json')
        self.base = ModeloEntrenado.objects.create(
            fase=ModeloEntrenado.Fase.BASE, hiperparametros=HIPERPARAMETROS_BASE,
            mae=1, rmse=1, smape=0.1, r2=0.9, ruta_archivo=ruta, activo=True,
        )
        self.vigente = ModeloEntrenado.objects.create(
            fase=ModeloEntrenado.Fase.AJUSTADO, nivel='categoria', hiperparametros={},
            mae=1, rmse=1, smape=0.1, r2=0.4, ruta_archivo=ruta, activo=True,
            origen_datos_internos=Origen.REAL, dias_entrenamiento=24, dias_prueba=7,
        )
        self.inactivo = ModeloEntrenado.objects.create(
            fase=ModeloEntrenado.Fase.AJUSTADO, nivel='categoria', hiperparametros={},
            mae=1, rmse=1, smape=0.1, r2=0.3, ruta_archivo=ruta, activo=False,
        )

    def _correr(self):
        salida = StringIO()
        dir_reportes = Path(self.tmp.name) / 'evaluacion'
        with mock.patch.object(evaluar_predicciones, 'leer_demanda_interna_por_categoria', return_value=self.interno), \
                mock.patch.object(evaluar_predicciones, 'DIR_REPORTES', dir_reportes):
            call_command('evaluar_predicciones', stdout=salida)
        return salida.getvalue(), dir_reportes

    def test_no_activa_ni_registra_ningun_modelo(self):
        antes = list(ModeloEntrenado.objects.order_by('pk').values_list('pk', 'fase', 'activo', 'r2'))
        salida, dir_reportes = self._correr()

        despues = list(ModeloEntrenado.objects.order_by('pk').values_list('pk', 'fase', 'activo', 'r2'))
        self.assertEqual(antes, despues)
        self.assertIn('No se activó ni registró ningún modelo', salida)
        self.assertEqual(len(list(dir_reportes.glob('evaluacion_categoria_*.json'))), 1)

    def test_guarda_la_corrida_en_el_modelo_vigente_y_en_json(self):
        salida, dir_reportes = self._correr()
        self.vigente.refresh_from_db()
        reporte = json.loads(next(dir_reportes.glob('*.json')).read_text())

        self.assertIsNotNone(self.vigente.fecha_evaluacion)
        self.assertAlmostEqual(self.vigente.wape, reporte['resultados']['modelo']['total']['wape'])
        self.assertAlmostEqual(self.vigente.exactitud, 1 - self.vigente.wape)
        self.assertEqual(set(self.vigente.r2_intra_categoria), {'accesorio', 'esmalte', 'latex'})
        self.assertEqual(set(self.vigente.acierto_tolerancia), {'accesorio', 'esmalte', 'latex'})
        self.assertIn(self.vigente.mejor_linea_base, LINEAS_BASE)
        self.assertEqual(self.vigente.mejor_linea_base, reporte['mejor_linea_base'])
        self.assertEqual(reporte['dias_prueba'], 7)
        self.assertEqual(reporte['fechas_prueba'], ['2026-08-25', '2026-08-31'])
        self.assertEqual(len(reporte['predicciones']), 21)
        self.assertIsNone(ModeloEntrenado.objects.get(pk=self.inactivo.pk).fecha_evaluacion)
        self.assertIn('R² global', salida)
        self.assertIn('Media móvil 7 días', salida)


class CompletarDemandaRealTests(TestCase):
    """La demanda real se completa solo con pedidos del origen pedido y
    solo hasta el último día con pedidos de ese origen."""

    def setUp(self):
        self.producto = Producto.objects.create(
            codigo='LAT-A', nombre='Latex A', categoria=Categoria.LATEX,
            precio_venta='10.00', costo_compra='5.00',
        )
        self.modelo = ModeloEntrenado.objects.create(
            fase=ModeloEntrenado.Fase.BASE, hiperparametros={}, mae=1, rmse=1, smape=0.1, r2=0.5,
            ruta_archivo='x.json', activo=False,
        )
        hoy = timezone.localdate()
        self.dia_real = hoy - timedelta(days=10)
        self.dia_prueba = hoy - timedelta(days=5)
        for dia, origen, cantidad in [(self.dia_real, Origen.REAL, 4), (self.dia_prueba, Origen.PRUEBA, 9)]:
            pedido = Pedido.objects.create(
                fecha_solicitud=timezone.make_aware(datetime.combine(dia, datetime.min.time())),
                cliente='Cliente', canal=Pedido.Canal.MOSTRADOR, estado=Pedido.Estado.ATENDIDO,
                origen=origen,
            )
            PedidoDetalle.objects.create(pedido=pedido, producto=self.producto, cantidad_solicitada=cantidad)
        for dia in (self.dia_real, self.dia_prueba):
            Prediccion.objects.create(
                producto=self.producto, fecha_generacion=timezone.now(), fecha_objetivo=dia,
                demanda_predicha=3, modelo=self.modelo,
            )

    def test_no_usa_pedidos_de_otro_origen_ni_completa_dias_sin_datos(self):
        call_command('evaluar_predicciones', stdout=StringIO())
        self.assertEqual(Prediccion.objects.get(fecha_objetivo=self.dia_real).demanda_real, 4)
        # Posterior al último pedido real: sigue sin demanda real (no se pone 0
        # ni se toma el pedido de prueba).
        self.assertIsNone(Prediccion.objects.get(fecha_objetivo=self.dia_prueba).demanda_real)
