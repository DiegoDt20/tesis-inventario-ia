"""Tests del motor de predicción de demanda (inventario/ml).

No requieren base de datos: construir_features, dividir_temporal y el
entrenamiento trabajan solo sobre DataFrames y modelos en memoria/disco.
"""
import tempfile
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd
from django.test import SimpleTestCase

from inventario.ml import entrenamiento
from inventario.ml.entrenamiento import (
    HIPERPARAMETROS_AJUSTE,
    HIPERPARAMETROS_BASE,
    entrenar_fase_ajuste,
    entrenar_fase_base,
    guardar_modelo,
)
from inventario.ml.evaluacion import comparar_modelos
from inventario.ml.features import COLUMNAS_SALIDA, construir_features, dividir_temporal


def _serie_sintetica(serie_id, dias, inicio='2020-01-01', valor=None):
    """Serie diaria simple: por defecto cantidad = índice del día (0, 1, 2...),
    útil para verificar a mano de dónde sale cada rezago."""
    fechas = pd.date_range(inicio, periods=dias, freq='D')
    if valor is None:
        cantidades = np.arange(dias, dtype=float)
    else:
        cantidades = valor(dias)
    return pd.DataFrame({'fecha': fechas, 'serie_id': serie_id, 'cantidad': cantidades})


class ConstruirFeaturesTests(SimpleTestCase):
    def test_rezago_7_no_usa_datos_futuros(self):
        df = _serie_sintetica('A', 60)
        features = construir_features(df)

        # rezago_7 del día i debe ser exactamente cantidad[i-7], nunca un
        # valor de un día posterior.
        for i in range(7, 60):
            esperado = float(i - 7)
            fila = features[features['fecha'] == df.loc[i, 'fecha']]
            self.assertEqual(fila['rezago_7'].iloc[0], esperado)

        # Los primeros 7 días no tienen 7 días de historia: deben ser NaN.
        primeras_filas = features.sort_values('fecha').head(7)
        self.assertTrue(primeras_filas['rezago_7'].isna().all())

    def test_cambiar_el_futuro_no_altera_features_pasadas(self):
        """Prueba directa de fuga temporal: dos series idénticas hasta el
        día 40 pero distintas después no pueden producir features distintas
        para los días <= 40."""
        base = _serie_sintetica('A', 80)

        alterada = base.copy()
        alterada.loc[40:, 'cantidad'] = alterada.loc[40:, 'cantidad'] + 1000

        features_base = construir_features(base)
        features_alterada = construir_features(alterada)

        columnas_derivadas = [c for c in COLUMNAS_SALIDA if c not in ('fecha', 'serie_id', 'cantidad')]
        primeros_40 = features_base['fecha'] < base.loc[40, 'fecha']

        pd.testing.assert_frame_equal(
            features_base.loc[primeros_40, columnas_derivadas].reset_index(drop=True),
            features_alterada.loc[primeros_40, columnas_derivadas].reset_index(drop=True),
        )

    def test_dias_sin_venta_no_se_omiten(self):
        """Si el origen de datos no trae filas para un día, construir_features
        debe completarlo con cantidad cero en vez de dejar un hueco."""
        df = pd.DataFrame({
            'fecha': pd.to_datetime(['2021-01-01', '2021-01-05']),
            'serie_id': ['A', 'A'],
            'cantidad': [10, 20],
        })
        features = construir_features(df)
        self.assertEqual(len(features), 5)
        dias_intermedios = features[
            features['fecha'].isin(pd.to_datetime(['2021-01-02', '2021-01-03', '2021-01-04']))
        ]
        self.assertTrue((dias_intermedios['cantidad'] == 0).all())

    def test_media_movil_no_incluye_el_dia_actual(self):
        """La media móvil de 7 días del día i se calcula con los días
        i-7..i-1: si el propio día i se filtrara, un salto grande en la
        cantidad del día i cambiaría su propia media, lo cual no debe pasar."""
        df = _serie_sintetica('A', 20, valor=lambda n: np.zeros(n))
        df.loc[10, 'cantidad'] = 10_000  # un solo pico, aislado
        features = construir_features(df)
        fila_pico = features[features['fecha'] == df.loc[10, 'fecha']].iloc[0]
        self.assertEqual(fila_pico['media_movil_7'], 0.0)


class DividirTemporalTests(SimpleTestCase):
    def test_respeta_orden_cronologico_sin_fuga(self):
        df = construir_features(_serie_sintetica('A', 100))
        train, test = dividir_temporal(df, dias_test=30)

        self.assertEqual(len(train) + len(test), len(df))
        self.assertLess(train['fecha'].max(), test['fecha'].min())
        self.assertEqual(test['fecha'].nunique(), 30)

    def test_no_usa_train_test_split_con_shuffle(self):
        """dividir_temporal no debe barajar filas: el test debe ser
        exactamente la cola cronológica del DataFrame, no una muestra
        aleatoria (lo cual infla las métricas al filtrar el futuro al
        pasado)."""
        df = construir_features(_serie_sintetica('A', 50))
        _, test = dividir_temporal(df, dias_test=10)
        fechas_test_ordenadas = sorted(df['fecha'].unique())[-10:]
        self.assertEqual(sorted(test['fecha'].unique().tolist()), fechas_test_ordenadas)


class ColumnasIdenticasTests(SimpleTestCase):
    def test_externa_e_interna_producen_las_mismas_columnas(self):
        """El conjunto de variables debe ser idéntico entre el dataset
        externo (serie_id de texto, p. ej. 'store-item') y el interno
        (serie_id numérico, el id del producto): la transferencia de
        aprendizaje depende de que ambos usen exactamente las mismas
        columnas en el mismo orden."""
        externo = _serie_sintetica('3-27', 45)
        interno = _serie_sintetica(101, 45, inicio='2026-08-01')

        columnas_externo = construir_features(externo).columns.tolist()
        columnas_interno = construir_features(interno).columns.tolist()

        self.assertEqual(columnas_externo, columnas_interno)
        self.assertEqual(columnas_externo, COLUMNAS_SALIDA)


class PipelineCompletoTests(SimpleTestCase):
    def test_fase_base_y_fase_ajuste_corren_de_punta_a_punta(self):
        rng = np.random.default_rng(42)

        def demanda_externa(n):
            return rng.poisson(lam=5, size=n).astype(float)

        def demanda_interna(n):
            return rng.poisson(lam=3, size=n).astype(float)

        df_externo = pd.concat([
            _serie_sintetica(f'{tienda}-{item}', 200, valor=demanda_externa)
            for tienda in (1, 2) for item in (1, 2)
        ], ignore_index=True)

        df_interno = pd.concat([
            _serie_sintetica(serie_id, 90, inicio='2026-01-01', valor=demanda_interna)
            for serie_id in (1, 2)
        ], ignore_index=True)

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(entrenamiento, 'DIR_MODELOS', Path(tmp)):
                modelo_base, train_ext, test_ext, hp_base = entrenar_fase_base(df_externo, dias_test=15)
                self.assertEqual(hp_base, HIPERPARAMETROS_BASE)
                self.assertGreater(len(train_ext), 0)
                self.assertGreater(len(test_ext), 0)

                ruta_base = guardar_modelo(modelo_base, 'base_test.json')
                self.assertTrue(Path(ruta_base).exists())

                modelo_ajustado, train_int, test_int, hp_ajuste = entrenar_fase_ajuste(
                    df_interno, ruta_base, dias_test=15,
                )
                self.assertEqual(hp_ajuste, HIPERPARAMETROS_AJUSTE)

                comparacion = comparar_modelos(train_int, test_int, modelo_ajustado)

        for clave in ('linea_base', 'solo_interno', 'ajustado'):
            self.assertIn(clave, comparacion)
            for metrica in ('mae', 'rmse', 'smape', 'r2'):
                valor = comparacion[clave][metrica]
                self.assertTrue(np.isfinite(valor), f'{clave}.{metrica} no es finito: {valor}')
