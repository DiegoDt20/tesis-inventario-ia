"""Tests de la optimización del motor de predicción (inventario/ml/optimizacion.py
y el comando optimizar_modelo).

Lo que se verifica es la validez de la evaluación, no qué configuración
gana: que la validación cruzada solo use días de entrenamiento y en orden
cronológico, que el conjunto de prueba no influya en ninguna elección, que
las variables experimentales no filtren el futuro y que el comando no
active ningún modelo.
"""
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from inventario.management.commands import optimizar_modelo
from inventario.ml import optimizacion
from inventario.ml.entrenamiento import HIPERPARAMETROS_BASE, entrenar_modelo, guardar_modelo
from inventario.ml.features import (
    COLUMNAS_EXPERIMENTALES,
    COLUMNAS_FEATURES,
    COLUMNAS_SALIDA,
    construir_features,
    dividir_temporal,
)
from inventario.ml.optimizacion import (
    Optimizador,
    particiones_por_fecha,
    predecir_recursivo,
)
from inventario.models import ModeloEntrenado


def _serie(serie_id, dias, inicio='2026-01-01', semilla=0, lam=5):
    rng = np.random.default_rng(semilla)
    return pd.DataFrame({
        'fecha': pd.date_range(inicio, periods=dias, freq='D'),
        'serie_id': serie_id,
        'cantidad': rng.poisson(lam=lam, size=dias).astype(float),
    })


def _datos_sinteticos():
    """Externo: 4 series de 120 días. Interno: 3 series de 31 días (como
    agosto con datos reales)."""
    externo = pd.concat(
        [_serie(f'1-{i}', 120, inicio='2015-01-01', semilla=i) for i in range(4)], ignore_index=True,
    )
    interno = pd.concat(
        [_serie(nombre, 31, inicio='2026-08-01', semilla=10 + i, lam=10 * (i + 1))
         for i, nombre in enumerate(['accesorio', 'esmalte', 'latex'])],
        ignore_index=True,
    )
    return externo, interno


# Variantes de base y búsqueda reducidas para que los tests corran rápido.
PARCHE_RAPIDO = {
    'ARBOLES_BASE': [20],
    'LEARNING_RATES_BASE': [0.1],
}


class VariablesExperimentalesTests(SimpleTestCase):
    def test_por_defecto_no_cambia_las_columnas(self):
        features = construir_features(_serie('A', 40))
        self.assertEqual(features.columns.tolist(), COLUMNAS_SALIDA)

    def test_rezago_1_y_media_movil_3_solo_usan_dias_anteriores(self):
        df = pd.DataFrame({
            'fecha': pd.date_range('2026-01-01', periods=10, freq='D'),
            'serie_id': 'A',
            'cantidad': np.arange(10, dtype=float),
        })
        features = construir_features(df, experimentales=True).set_index('fecha')
        dia = pd.Timestamp('2026-01-06')  # cantidad 5
        self.assertEqual(features.loc[dia, 'rezago_1'], 4)
        self.assertEqual(features.loc[dia, 'media_movil_3'], np.mean([2, 3, 4]))

    def test_cambiar_el_futuro_no_altera_variables_experimentales_pasadas(self):
        df = _serie('A', 40)
        alterado = df.copy()
        alterado.loc[alterado['fecha'] >= '2026-01-30', 'cantidad'] = 999
        antes = construir_features(df, experimentales=True)
        despues = construir_features(alterado, experimentales=True)
        pasado = antes['fecha'] < '2026-01-30'
        pd.testing.assert_frame_equal(
            antes.loc[pasado, COLUMNAS_EXPERIMENTALES], despues.loc[pasado, COLUMNAS_EXPERIMENTALES],
        )

    def test_inicio_y_fin_de_mes(self):
        df = pd.DataFrame({
            'fecha': pd.date_range('2026-02-01', '2026-03-06', freq='D'),
            'serie_id': 'A', 'cantidad': 1.0,
        })
        features = construir_features(df, experimentales=True).set_index('fecha')
        self.assertEqual(features.loc['2026-02-05', 'es_inicio_mes'], 1)
        self.assertEqual(features.loc['2026-02-06', 'es_inicio_mes'], 0)
        # Febrero 2026 tiene 28 días: el fin de mes son los días 24 a 28.
        self.assertEqual(features.loc['2026-02-23', 'es_fin_mes'], 0)
        self.assertEqual(features.loc['2026-02-24', 'es_fin_mes'], 1)
        self.assertEqual(features.loc['2026-03-01', 'es_fin_mes'], 0)


class ParticionesPorFechaTests(SimpleTestCase):
    def test_validacion_siempre_posterior_y_sin_partir_un_dia(self):
        _, interno = _datos_sinteticos()
        train, test = dividir_temporal(construir_features(interno), dias_test=7)
        particiones = particiones_por_fecha(train, n_pliegues=4)

        self.assertEqual(len(particiones), 4)
        for idx_ent, idx_val in particiones:
            fechas_ent = train.loc[idx_ent, 'fecha']
            fechas_val = train.loc[idx_val, 'fecha']
            self.assertLess(fechas_ent.max(), fechas_val.min())
            self.assertTrue(set(fechas_ent).isdisjoint(set(fechas_val)))
            # Cada fecha de validación trae todas sus series.
            self.assertEqual(len(idx_val), fechas_val.nunique() * train['serie_id'].nunique())
            # Ningún pliegue toca el conjunto de prueba.
            self.assertLess(fechas_val.max(), test['fecha'].min())


class PrediccionRecursivaTests(SimpleTestCase):
    def test_no_usa_la_demanda_real_del_periodo_pronosticado(self):
        externo, interno = _datos_sinteticos()
        modelo = entrenar_modelo(construir_features(externo), {**HIPERPARAMETROS_BASE, 'n_estimators': 20})
        corte = pd.Timestamp('2026-08-25')
        historial = interno[interno['fecha'] < corte]
        fechas = list(pd.date_range(corte, '2026-08-31', freq='D'))

        predicciones = predecir_recursivo(modelo, historial, fechas, COLUMNAS_FEATURES, False)

        self.assertEqual(len(predicciones), 7 * 3)
        self.assertTrue((predicciones['prediccion'] >= 0).all())
        self.assertGreaterEqual(predicciones['fecha'].min(), corte)


class OptimizadorTests(SimpleTestCase):
    def _optimizar(self, interno):
        externo, _ = _datos_sinteticos()
        with mock.patch.multiple(optimizacion, **PARCHE_RAPIDO):
            optimizador = Optimizador(interno, externo, dias_test=7, n_pliegues=3, n_iteraciones=4)
            filas = optimizador.ejecutar()
        return optimizador, filas

    def test_el_test_no_influye_en_ninguna_eleccion(self):
        """Cambiar por completo la demanda de los días de prueba no debe
        cambiar nada de lo que se elige: ni los resultados de la validación
        cruzada, ni los hiperparámetros elegidos, ni las variables quitadas."""
        _, interno = _datos_sinteticos()
        alterado = interno.copy()
        en_test = alterado['fecha'] >= '2026-08-25'
        alterado.loc[en_test, 'cantidad'] = alterado.loc[en_test, 'cantidad'] * 7 + 50

        original, filas_original = self._optimizar(interno)
        con_test_alterado, filas_alteradas = self._optimizar(alterado)

        self.assertEqual(
            [c['hiperparametros'] for c in original.candidatos_ajuste],
            [c['hiperparametros'] for c in con_test_alterado.candidatos_ajuste],
        )
        self.assertEqual(original.variables_quitadas, con_test_alterado.variables_quitadas)
        cv_original = {f['nombre']: f['cv'] for f in filas_original}
        cv_alterado = {f['nombre']: f['cv'] for f in filas_alteradas}
        self.assertEqual(cv_original, cv_alterado)
        # Lo único que cambia es la medición en test.
        self.assertNotEqual(
            {f['nombre']: f['test']['r2'] for f in filas_original},
            {f['nombre']: f['test']['r2'] for f in filas_alteradas},
        )

    def test_tabla_completa_y_ordenada_por_r2_en_test(self):
        _, interno = _datos_sinteticos()
        optimizador, filas = self._optimizar(interno)

        nombres = [f['nombre'] for f in filas]
        self.assertIn('Configuración actual (vigente)', nombres)
        self.assertIn('Ajuste optimizado por CV', nombres)
        self.assertIn('Línea base ingenua (demanda del día anterior)', nombres)
        r2 = [f['test']['r2'] for f in filas]
        self.assertEqual(r2, sorted(r2, reverse=True))
        for fila in filas:
            self.assertEqual(len(optimizador.test), 21)
            for valor in (fila['cv']['r2'], fila['test']['r2'], fila['r2_recursivo']):
                self.assertTrue(np.isfinite(valor), f'{fila["nombre"]}: {valor}')
        # La actual siempre es candidata de la búsqueda.
        self.assertIn(
            {**optimizacion.HIPERPARAMETROS_AJUSTE, 'random_state': 42},
            [c['hiperparametros'] for c in optimizador.candidatos_ajuste],
        )

    def test_variantes_de_variables_reentrenan_la_base_con_las_mismas_columnas(self):
        _, interno = _datos_sinteticos()
        _, filas = self._optimizar(interno)
        for fila in filas:
            modelo = fila.get('modelo')
            if modelo is not None:
                self.assertEqual(modelo.get_booster().feature_names, fila['columnas'])


class ComandoOptimizarModeloTests(TestCase):
    def test_no_activa_ni_registra_modelos(self):
        externo, interno = _datos_sinteticos()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch('inventario.ml.entrenamiento.DIR_MODELOS', Path(tmp)):
                base = entrenar_modelo(construir_features(externo), {**HIPERPARAMETROS_BASE, 'n_estimators': 20})
                ruta = guardar_modelo(base, 'base.json')
            ModeloEntrenado.objects.create(
                fase=ModeloEntrenado.Fase.BASE, hiperparametros=HIPERPARAMETROS_BASE,
                mae=1, rmse=1, smape=0.1, r2=0.5, ruta_archivo=ruta, activo=True,
            )
            vigente = ModeloEntrenado.objects.create(
                fase=ModeloEntrenado.Fase.AJUSTADO, nivel='categoria', hiperparametros={},
                mae=1, rmse=1, smape=0.1, r2=0.4, ruta_archivo=ruta, activo=True,
            )
            estado_antes = list(ModeloEntrenado.objects.order_by('pk').values_list('pk', 'activo'))

            salida = StringIO()
            with mock.patch.multiple(optimizacion, **PARCHE_RAPIDO), \
                    mock.patch.object(optimizar_modelo, 'leer_demanda_interna_por_categoria', return_value=interno), \
                    mock.patch.object(optimizar_modelo, 'leer_datos_externos', return_value=externo), \
                    mock.patch.object(optimizar_modelo, 'DIR_REPORTES', Path(tmp) / 'reportes'):
                call_command(
                    'optimizar_modelo', '--iteraciones', '3', '--pliegues', '3',
                    '--semillas-ruido', '2', stdout=salida,
                )
            reportes = list((Path(tmp) / 'reportes').iterdir())

        self.assertEqual(
            list(ModeloEntrenado.objects.order_by('pk').values_list('pk', 'activo')), estado_antes,
        )
        self.assertIn(f'el ajustado vigente sigue siendo #{vigente.pk}', salida.getvalue())
        self.assertEqual(sorted(r.suffix for r in reportes), ['.csv', '.json'])
