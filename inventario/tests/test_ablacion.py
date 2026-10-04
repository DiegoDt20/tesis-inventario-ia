"""Tests del estudio de ablación (inventario/ml/ablacion.py y comando
evaluar_ablacion)."""
import json
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd
import xgboost as xgb
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from inventario.management.commands import evaluar_ablacion
from inventario.ml import ablacion, entrenamiento
from inventario.ml.entrenamiento import HIPERPARAMETROS_AJUSTE, HIPERPARAMETROS_BASE, entrenar_modelo, guardar_modelo
from inventario.ml.escalado import configuracion_guardada
from inventario.ml.features import COLUMNAS_FEATURES, construir_features
from inventario.models import ModeloEntrenado, Origen, Producto

from .test_evaluacion import _interno

HIPER_RAPIDOS = {**HIPERPARAMETROS_AJUSTE, 'n_estimators': 3}
# Con el fixture, accesorio (nivel 7) queda bajo el mínimo; esmalte (65) y
# látex (10), no.
NIVEL_MINIMO, NIVEL_REFERENCIA = 9.0, 50.0


def _base():
    return entrenar_modelo(construir_features(_interno()), {**HIPERPARAMETROS_BASE, 'n_estimators': 5})


def _validaciones(interno, base, entrenar=entrenar_modelo):
    return ablacion.construir_validaciones(interno, base, HIPER_RAPIDOS, NIVEL_MINIMO, NIVEL_REFERENCIA, entrenar)


class SoloExternoTests(SimpleTestCase):
    def test_a_no_entrena_con_ningun_dia_de_agosto(self):
        """Alterar la demanda de los días de ajuste no cambia el modelo que
        usa A en ningún origen: es siempre el base, con los mismos árboles y
        las mismas predicciones sobre una matriz fija. (El pronóstico de A sí
        cambia, porque el historial de agosto es su ENTRADA: eso es "sin
        ajuste con datos de la empresa", no "sin datos de la empresa".)"""
        base = _base()
        arboles_antes = bytes(base.get_booster().save_raw())
        sonda = construir_features(_interno())[COLUMNAS_FEATURES]
        prediccion_sonda = base.predict(sonda)

        interno = _interno()
        alterado = interno.copy()
        alterado['cantidad'] = alterado['cantidad'] * 10 + 500.0
        for datos in (interno, alterado):
            validacion = _validaciones(datos, base)['A']
            validacion.evaluar(7)
            for origen in validacion.origenes(7):
                self.assertIs(validacion.modelo(origen), base)

        self.assertEqual(bytes(base.get_booster().save_raw()), arboles_antes)
        np.testing.assert_array_equal(base.predict(sonda), prediccion_sonda)

    def test_el_escalado_solo_se_aplica_donde_se_declaro(self):
        validaciones = _validaciones(_interno(), _base())
        diario, _ = ablacion.evaluar(validaciones, 7)

        self.assertEqual(validaciones['A'].factores, {})
        self.assertEqual(validaciones['B'].factores, {})
        for clave in ('A_escalado', 'C'):
            factores = validaciones[clave].factores
            self.assertEqual(len(factores), 11)
            for por_serie in factores.values():
                self.assertGreater(por_serie['accesorio'], 1.0)
                self.assertEqual((por_serie['esmalte'], por_serie['latex']), (1.0, 1.0))
        # Con el mismo base, A y A con escalado solo difieren en la serie escalada.
        otras = diario['categoria'] != 'accesorio'
        np.testing.assert_allclose(diario.loc[otras, 'A'], diario.loc[otras, 'A_escalado'])
        self.assertFalse(np.allclose(diario.loc[~otras, 'A'], diario.loc[~otras, 'A_escalado']))


class SoloInternoTests(SimpleTestCase):
    def test_b_no_usa_el_modelo_base(self):
        """B entrena desde cero: nunca recibe el base como punto de partida
        ni carga su artefacto."""
        base = _base()
        llamadas = []

        def entrenar(train, hiperparametros, xgb_model=None):
            llamadas.append((hiperparametros, xgb_model))
            return entrenar_modelo(train, hiperparametros)

        validacion = _validaciones(_interno(), base, entrenar)['B']
        with mock.patch.object(xgb.XGBRegressor, 'load_model') as cargar, \
                mock.patch.object(xgb.Booster, 'load_model') as cargar_booster:
            validacion.evaluar(7)
        cargar.assert_not_called()
        cargar_booster.assert_not_called()
        self.assertEqual(len(llamadas), 11)
        self.assertTrue(all(xgb_model is None and hp == HIPER_RAPIDOS for hp, xgb_model in llamadas))
        for origen in validacion.origenes(7):
            self.assertIsNot(validacion.modelo(origen), base)


class MismasFilasTests(SimpleTestCase):
    def test_todas_las_configuraciones_sobre_los_mismos_origenes_y_filas(self):
        validaciones = _validaciones(_interno(), _base())
        origenes = {clave: v.origenes(7) for clave, v in validaciones.items()}
        self.assertEqual(len({tuple(o) for o in origenes.values()}), 1)

        diario, ventanas = ablacion.evaluar(validaciones, 7)
        self.assertEqual(len(diario), 11 * 7 * 3)
        self.assertEqual(len(ventanas), 11 * 3)
        self.assertFalse(diario[ablacion.COLUMNAS].isna().any().any())

    def test_filas_distintas_se_rechazan(self):
        filas = pd.DataFrame({
            'origen': pd.Timestamp('2026-08-14'), 'fecha': pd.to_datetime(['2026-08-15', '2026-08-16']),
            'categoria': 'A', 'real': [1.0, 2.0], 'modelo': [1.0, 1.0],
            'media_categoria': 1.0, 'media_movil_7': 1.0, 'ultimo_valor': 1.0,
        })
        diarios = {clave: filas for clave in ablacion.CONFIGURACIONES}
        diarios['B'] = filas.iloc[:1]
        with self.assertRaisesMessage(ValueError, 'La configuración B no se evaluó sobre las mismas filas'):
            ablacion.unir_configuraciones(diarios)


class ComandoAblacionTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.interno = _interno()
        with mock.patch.object(entrenamiento, 'DIR_MODELOS', Path(self.tmp.name)):
            ruta = guardar_modelo(_base(), 'base.json')
        for categoria in ['accesorio', 'esmalte', 'latex']:
            Producto.objects.create(
                codigo=f'P-{categoria}', nombre=categoria, categoria=categoria,
                precio_venta='10.00', costo_compra='5.00', lead_time_dias=7,
            )
        ModeloEntrenado.objects.create(
            fase=ModeloEntrenado.Fase.BASE, hiperparametros=HIPERPARAMETROS_BASE,
            mae=1, rmse=1, smape=0.1, r2=0.9, ruta_archivo=ruta, activo=True,
        )
        self.vigente = ModeloEntrenado.objects.create(
            fase=ModeloEntrenado.Fase.AJUSTADO, nivel='categoria', hiperparametros=HIPER_RAPIDOS,
            mae=1, rmse=1, smape=0.1, r2=0.4, ruta_archivo=ruta, activo=True,
            origen_datos_internos=Origen.REAL, dias_entrenamiento=24, dias_prueba=7,
            escalado=configuracion_guardada({'minimo': NIVEL_MINIMO, 'referencia': NIVEL_REFERENCIA}, {'accesorio': 6.0}),
        )

    def _correr(self):
        salida = StringIO()
        dir_reportes = Path(self.tmp.name) / 'ablacion'
        with mock.patch.object(evaluar_ablacion, 'leer_demanda_interna_por_categoria', return_value=self.interno), \
                mock.patch.object(evaluar_ablacion, 'DIR_REPORTES', dir_reportes):
            call_command('evaluar_ablacion', stdout=salida)
        return salida.getvalue(), json.loads(next(dir_reportes.glob('ablacion_*.json')).read_text())

    def test_reporte_con_las_seis_filas_las_comparaciones_y_la_lectura(self):
        antes = list(ModeloEntrenado.objects.order_by('pk').values_list('pk', 'activo', 'r2', 'ruta_archivo'))
        salida, reporte = self._correr()

        self.assertEqual(antes, list(ModeloEntrenado.objects.order_by('pk').values_list('pk', 'activo', 'r2', 'ruta_archivo')))
        for horizonte, n_origenes in (('diario', 17), ('ventana', 11)):
            seccion = reporte['horizontes'][horizonte]
            self.assertEqual(len(seccion['origenes']), n_origenes)
            self.assertEqual(set(seccion['resumen']['metodos']), set(ablacion.COLUMNAS))
            pares = {(c['configuracion'], c['contra']) for c in seccion['resumen']['comparaciones']}
            self.assertTrue({('C', 'A'), ('C', 'B'), ('A', 'B')} <= pares)
        self.assertEqual(len(reporte['lectura']['texto']), 3)
        self.assertIn('sin ajuste con datos de la empresa', reporte['precision'])
        for inicio in ('1. ¿Funciona sin ajuste', '2. ¿Funciona sin la transferencia', '3. ¿La combinación'):
            self.assertIn(inicio, salida)
        self.assertIn('los p indican consistencia, no son prueba de significancia', salida)
        self.assertIn('No se tocaron datos ni se activó ni registró ningún modelo', salida)

    def test_sin_escalado_en_el_vigente_falla_con_mensaje_claro(self):
        self.vigente.escalado = None
        self.vigente.save()
        with self.assertRaisesMessage(CommandError, 'no se ajustó con escalado de nivel'):
            self._correr()
