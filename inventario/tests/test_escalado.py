"""Tests del escalado de nivel en producción: entrenar_fase_ajuste_escalado,
entrenar_ajustado, predecir_demanda y evaluar_predicciones con un modelo
vigente escalado (inventario/ml/escalado.py)."""
import json
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from inventario.management.commands import entrenar_ajustado, evaluar_predicciones
from inventario.management.commands.predecir_demanda import Command as PredecirDemanda
from inventario.ml import entrenamiento
from inventario.ml.entrenamiento import (
    HIPERPARAMETROS_AJUSTE,
    HIPERPARAMETROS_BASE,
    entrenar_fase_ajuste_escalado,
    entrenar_modelo,
    guardar_modelo,
)
from inventario.ml.escalado import EscaladoNivel, configuracion_guardada, escalar, factor_guardado
from inventario.ml.evaluacion import comparar_modelos
from inventario.ml.features import construir_features, dividir_temporal
from inventario.models import ModeloEntrenado, Origen, Producto

from .test_diagnostico_sesgo import ModeloFijo, ModeloMediaMovil
from .test_evaluacion import _interno

# accesorio (nivel 7) queda bajo el mínimo; esmalte (65) y látex (10), no.
ESCALADO = EscaladoNivel(nivel_minimo=9.0, nivel_referencia=50.0)


class EntrenamientoEscaladoTests(SimpleTestCase):
    def _entrenar(self, interno, ruta_base):
        with mock.patch.object(entrenamiento, 'HIPERPARAMETROS_AJUSTE', {**HIPERPARAMETROS_AJUSTE, 'n_estimators': 3}):
            return entrenar_fase_ajuste_escalado(interno, ruta_base, ESCALADO, dias_test=7)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with mock.patch.object(entrenamiento, 'DIR_MODELOS', Path(self.tmp.name)):
            base = entrenar_modelo(construir_features(_interno()), {**HIPERPARAMETROS_BASE, 'n_estimators': 5})
            self.ruta_base = guardar_modelo(base, 'base.json')

    def test_los_factores_solo_usan_los_dias_de_entrenamiento(self):
        """Los 7 días de prueba no entran en el factor: llevarlos a 10.000
        no lo cambia (si entraran, accesorio quedaría sobre el mínimo)."""
        interno = _interno()
        alterado = interno.copy()
        alterado.loc[alterado['fecha'] >= '2026-08-25', 'cantidad'] = 10_000.0

        factores = self._entrenar(interno, self.ruta_base)[4]
        factores_alterado = self._entrenar(alterado, self.ruta_base)[4]
        self.assertEqual(factores, factores_alterado)
        media_entrenamiento = interno.loc[
            (interno['serie_id'] == 'accesorio') & (interno['fecha'] < '2026-08-25'), 'cantidad',
        ].mean()
        self.assertAlmostEqual(factores['accesorio'], 50.0 / media_entrenamiento)
        self.assertEqual((factores['esmalte'], factores['latex']), (1.0, 1.0))

    def test_test_en_escala_original_y_variables_del_modelo_escaladas_solo_en_la_serie_baja(self):
        interno = _interno()
        _, train, test, _, factores, test_modelo = self._entrenar(interno, self.ruta_base)
        _, test_original = dividir_temporal(construir_features(interno), dias_test=7)

        pd.testing.assert_frame_equal(test, test_original)
        for serie in ['esmalte', 'latex']:
            pd.testing.assert_frame_equal(
                test_modelo[test_modelo['serie_id'] == serie], test[test['serie_id'] == serie],
            )
        accesorio = test['serie_id'] == 'accesorio'
        np.testing.assert_allclose(
            test_modelo.loc[accesorio, 'rezago_7'], test.loc[accesorio, 'rezago_7'] * factores['accesorio'],
        )

    def test_comparar_modelos_desescala_el_pronostico_del_ajustado(self):
        """Con un modelo homogéneo (media móvil), el ajustado evaluado sobre
        las variables escaladas y desescalado da lo mismo que sin escalar."""
        interno = _interno()
        train, test = dividir_temporal(construir_features(interno), dias_test=7)
        factores = {'accesorio': 6.0}
        _, test_modelo = dividir_temporal(construir_features(escalar(interno, factores)), dias_test=7)

        sin = comparar_modelos(train, test, ModeloMediaMovil())
        con = comparar_modelos(train, test, ModeloMediaMovil(), test_modelo=test_modelo, factores=factores)
        self.assertAlmostEqual(sin['ajustado']['mae'], con['ajustado']['mae'])
        self.assertEqual(sin['linea_base'], con['linea_base'])


class PredecirDemandaEscaladaTests(SimpleTestCase):
    historial = pd.DataFrame({
        'fecha': pd.date_range('2026-08-01', periods=20, freq='D'), 'serie_id': 'accesorio', 'cantidad': 4.0,
    })

    def test_la_prediccion_vuelve_a_la_escala_original(self):
        # El modelo responde 30 en su escala; con factor 6 la predicción es 5.
        valores = [v for _, v in PredecirDemanda()._predecir_serie(self.historial, ModeloFijo([30.0]), 3, 6.0)]
        np.testing.assert_allclose(valores, [5.0, 5.0, 5.0])

    def test_con_modelo_homogeneo_el_factor_no_cambia_nada(self):
        comando = PredecirDemanda()
        sin = comando._predecir_serie(self.historial, ModeloMediaMovil(), 5)
        con = comando._predecir_serie(self.historial, ModeloMediaMovil(), 5, 6.0)
        np.testing.assert_allclose([v for _, v in sin], [v for _, v in con])

    def test_factor_guardado_con_claves_de_texto(self):
        configuracion = configuracion_guardada({'minimo': 9.0, 'referencia': 50.0}, {'accesorio': 6.0, 17: 2.0})
        self.assertEqual(factor_guardado(configuracion, 'accesorio'), 6.0)
        self.assertEqual(factor_guardado(configuracion, 17), 2.0)
        self.assertEqual(factor_guardado(configuracion, 'esmalte'), 1.0)
        self.assertEqual(factor_guardado(None, 'accesorio'), 1.0)


class ComandosConEscaladoTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.interno = _interno()
        parche = mock.patch.object(entrenamiento, 'DIR_MODELOS', Path(self.tmp.name))
        parche.start()
        self.addCleanup(parche.stop)
        base = entrenar_modelo(construir_features(self.interno), {**HIPERPARAMETROS_BASE, 'n_estimators': 5})
        self.base = ModeloEntrenado.objects.create(
            fase=ModeloEntrenado.Fase.BASE, hiperparametros=HIPERPARAMETROS_BASE,
            mae=1, rmse=1, smape=0.1, r2=0.9, ruta_archivo=guardar_modelo(base, 'base.json'), activo=True,
        )
        for categoria in ['accesorio', 'esmalte', 'latex']:
            Producto.objects.create(
                codigo=f'P-{categoria}', nombre=categoria, categoria=categoria,
                precio_venta='10.00', costo_compra='5.00', lead_time_dias=7,
            )
        # Dataset externo: medias por serie 9, 50 y 80 → mínimo 9, mediana 50.
        self.externo = pd.DataFrame({
            'fecha': list(pd.date_range('2015-01-01', periods=4)) * 3,
            'serie_id': ['1-1'] * 4 + ['1-2'] * 4 + ['1-3'] * 4,
            'cantidad': [9.0] * 4 + [50.0] * 4 + [80.0] * 4,
        })
        self.archivo_externo = Path(self.tmp.name) / 'train.csv'
        self.archivo_externo.touch()

    def _entrenar(self, *argumentos):
        salida = StringIO()
        with mock.patch.object(entrenar_ajustado, 'leer_demanda_interna_por_categoria', return_value=self.interno), \
                mock.patch.object(entrenar_ajustado, 'leer_datos_externos', return_value=self.externo), \
                mock.patch.object(entrenamiento, 'HIPERPARAMETROS_AJUSTE', {**HIPERPARAMETROS_AJUSTE, 'n_estimators': 3}):
            call_command(
                'entrenar_ajustado', '--nivel', 'categoria', '--dias-test', '7',
                '--archivo-externo', str(self.archivo_externo), *argumentos, stdout=salida,
            )
        return salida.getvalue(), ModeloEntrenado.objects.get(fase=ModeloEntrenado.Fase.AJUSTADO, activo=True)

    def test_entrenar_ajustado_escala_por_defecto_y_guarda_la_configuracion(self):
        salida, modelo = self._entrenar()
        self.assertEqual(modelo.escalado['nivel_minimo'], 9.0)
        self.assertEqual(modelo.escalado['nivel_referencia'], 50.0)
        self.assertGreater(modelo.escalado['factores']['accesorio'], 1.0)
        self.assertEqual(modelo.escalado['factores']['esmalte'], 1.0)
        self.assertEqual(modelo.escalado['factores']['latex'], 1.0)
        self.assertIn('Series escaladas: accesorio', salida)

    def test_categoria_sin_pedidos_el_ultimo_dia_no_pierde_ese_dia_del_test(self):
        ultimo = self.interno['fecha'].max()
        self.interno = self.interno[~((self.interno['serie_id'] == 'esmalte') & (self.interno['fecha'] == ultimo))]
        salida, _ = self._entrenar()
        self.assertIn('Filas de test: 21', salida)

    def test_no_escalar_nivel_reproduce_el_procedimiento_anterior(self):
        _, modelo = self._entrenar('--no-escalar-nivel')
        self.assertIsNone(modelo.escalado)

    def test_sin_dataset_externo_falla_con_mensaje_claro(self):
        self.archivo_externo.unlink()
        with self.assertRaisesMessage(CommandError, '--no-escalar-nivel'):
            self._entrenar()

    def test_evaluar_predicciones_usa_el_escalado_del_vigente(self):
        _, modelo = self._entrenar()
        modelo.hiperparametros = {**HIPERPARAMETROS_AJUSTE, 'n_estimators': 3}
        modelo.origen_datos_internos = Origen.REAL
        modelo.save()
        salida = StringIO()
        dir_reportes = Path(self.tmp.name) / 'evaluacion'
        with mock.patch.object(evaluar_predicciones, 'leer_demanda_interna_por_categoria', return_value=self.interno), \
                mock.patch.object(evaluar_predicciones, 'DIR_REPORTES', dir_reportes), \
                mock.patch.object(evaluar_predicciones, 'ValidacionOrigenMovil',
                                  wraps=evaluar_predicciones.ValidacionOrigenMovil) as validacion:
            call_command('evaluar_predicciones', '--horizonte', 'ventana', stdout=salida)
        escalado = validacion.call_args.kwargs['escalado']
        self.assertEqual((escalado.nivel_minimo, escalado.nivel_referencia), (9.0, 50.0))
        self.assertIn('Con escalado de nivel, como el vigente', salida.getvalue())
        reporte = json.loads(next(dir_reportes.glob('*.json')).read_text())
        self.assertEqual(reporte['horizontes']['ventana']['n_origenes'], 11)

        # División única: con los factores guardados y en la escala original.
        salida = StringIO()
        with mock.patch.object(evaluar_predicciones, 'leer_demanda_interna_por_categoria', return_value=self.interno), \
                mock.patch.object(evaluar_predicciones, 'DIR_REPORTES', dir_reportes):
            call_command('evaluar_predicciones', '--no-walk-forward', '--horizonte', 'diario', stdout=salida)
        modelo.refresh_from_db()
        self.assertIsNotNone(modelo.wape)
        real = self.interno.loc[self.interno['fecha'] >= '2026-08-25', 'cantidad'].sum()
        reporte = json.loads(sorted(dir_reportes.glob('evaluacion_division_unica_*.json'))[-1].read_text())
        pronosticado = reporte['horizontes']['diario']['resultados']['modelo']['total']['demanda_pronosticada']
        # Del orden de la demanda real, no ~6 veces más en accesorio.
        self.assertLess(pronosticado, real * 2)
