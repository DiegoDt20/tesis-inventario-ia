"""Tests del diagnóstico de sesgo (inventario/ml/diagnostico_sesgo.py), del
escalado de nivel (inventario/ml/escalado.py) y del comando
diagnosticar_sesgo."""
import json
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from inventario.management.commands import diagnosticar_sesgo
from inventario.ml import diagnostico_sesgo as diag
from inventario.ml.entrenamiento import HIPERPARAMETROS_AJUSTE, HIPERPARAMETROS_BASE, entrenar_modelo, guardar_modelo
from inventario.ml.escalado import EscaladoNivel, escalar, factores_escala, invertir, niveles_externos
from inventario.ml.features import construir_features
from inventario.ml.optimizacion import predecir_recursivo
from inventario.ml.walk_forward import METODOS, ValidacionOrigenMovil
from inventario.models import ModeloEntrenado, Origen, Producto

from .test_evaluacion import _interno


class ModeloFijo:
    """Doble de modelo: devuelve siempre los mismos valores, en orden."""

    def __init__(self, valores):
        self.valores = list(valores)

    def predict(self, X):
        return np.array(self.valores[:len(X)], dtype=float)


class ModeloMediaMovil:
    """Doble de modelo homogéneo de grado 1: pronostica la media móvil de 7
    días. Multiplicar la serie por k multiplica su pronóstico por k, así que
    escalar y desescalar no debe cambiar nada."""

    def predict(self, X):
        return X['media_movil_7'].fillna(0).to_numpy(dtype=float)


class ErrorConSignoTests(SimpleTestCase):
    def test_distingue_sobreestimacion_de_subestimacion(self):
        # A: pronóstico 12 y 14 contra 10 y 10 → +2 y +4, sesgo +6/20.
        # B: pronóstico 5 contra 10 → -5, sesgo -0.5.
        filas = pd.DataFrame({
            'categoria': ['A', 'A', 'B'],
            'real': [10.0, 10.0, 10.0],
            'modelo': [12.0, 14.0, 5.0],
        })
        np.testing.assert_allclose(diag.error_con_signo(filas['real'], filas['modelo']), [2.0, 4.0, -5.0])
        resumen = diag.resumen_sesgo(filas)

        self.assertAlmostEqual(resumen['A']['error_medio'], 3.0)
        self.assertAlmostEqual(resumen['A']['sesgo_relativo'], 0.3)
        self.assertEqual((resumen['A']['positivas'], resumen['A']['negativas']), (2, 0))
        self.assertAlmostEqual(resumen['B']['error_medio'], -5.0)
        self.assertAlmostEqual(resumen['B']['sesgo_relativo'], -0.5)
        self.assertEqual((resumen['B']['positivas'], resumen['B']['negativas']), (0, 1))

    def test_errores_de_signo_contrario_se_compensan_en_el_sesgo(self):
        # El WAPE sería 10/20; el sesgo es 0: no hay sesgo, solo dispersión.
        self.assertEqual(diag.sesgo_relativo([10.0, 10.0], [15.0, 5.0]), 0.0)
        self.assertIsNone(diag.sesgo_relativo([0.0, 0.0], [1.0, 2.0]))


class ErrorPorPosicionTests(SimpleTestCase):
    def test_se_calcula_por_posicion_y_no_por_fecha(self):
        # El 16/08 es el día 2 del origen 14/08 y el día 1 del origen 15/08.
        o1, o2 = pd.Timestamp('2026-08-14'), pd.Timestamp('2026-08-15')
        diario = pd.DataFrame({
            'origen': [o1, o1, o2, o2],
            'fecha': pd.to_datetime(['2026-08-15', '2026-08-16', '2026-08-16', '2026-08-17']),
            'categoria': 'A',
            'real': [10.0, 10.0, 10.0, 10.0],
            'modelo': [11.0, 20.0, 13.0, 30.0],
        })
        posiciones = diag.error_por_posicion(diario)['A']

        # Día 1: errores +1 (15/08 desde o1) y +3 (16/08 desde o2) → +2.
        # Día 2: +10 (16/08 desde o1) y +20 (17/08 desde o2) → +15.
        self.assertEqual(posiciones, {1: 2.0, 2: 15.0})
        # Por fecha, el 16/08 promediaría +10 y +3: no es lo que se mide.
        self.assertNotIn(6.5, posiciones.values())


class ConteoRecortesTests(SimpleTestCase):
    def test_cuenta_y_suma_las_predicciones_negativas(self):
        diario = pd.DataFrame({
            'categoria': ['A', 'A', 'A', 'A', 'B'],
            'real': [1.0, 3.0, 0.0, 2.0, 5.0],
            'modelo_crudo': [-2.0, 3.0, -0.5, 0.0, 4.0],
        })
        diario['modelo'] = diario['modelo_crudo'].clip(lower=0)
        recortes = diag.conteo_recortes(diario)

        self.assertEqual(recortes['A']['predicciones'], 4)
        self.assertEqual(recortes['A']['recortadas'], 2)  # el 0.0 exacto no es un recorte
        self.assertAlmostEqual(recortes['A']['suma_recortada'], 2.5)
        # Con recorte: (0 + 3 + 0 + 0) - 6 = -3; sin recorte: 0.5 - 6 = -5.5.
        self.assertAlmostEqual(recortes['A']['sesgo_con_recorte'], -3 / 6)
        self.assertAlmostEqual(recortes['A']['sesgo_sin_recorte'], -5.5 / 6)
        self.assertEqual(recortes['B']['recortadas'], 0)
        self.assertEqual(recortes['B']['suma_recortada'], 0.0)

    def test_pronostico_encadenado_entrega_la_salida_cruda_y_encadena_con_piso(self):
        historial = pd.DataFrame({
            'fecha': pd.date_range('2026-08-01', periods=10, freq='D'), 'serie_id': 'A', 'cantidad': 1.0,
        })
        fechas = list(pd.date_range('2026-08-11', periods=2, freq='D'))
        pronostico = predecir_recursivo(ModeloFijo([-3.0]), historial, fechas, ['media_movil_7'], False,
                                        con_cruda=True)
        np.testing.assert_allclose(pronostico['prediccion_cruda'], [-3.0, -3.0])
        np.testing.assert_allclose(pronostico['prediccion'], [0.0, 0.0])
        self.assertNotIn(
            'prediccion_cruda', predecir_recursivo(ModeloFijo([1.0]), historial, fechas, ['media_movil_7'], False),
        )


class IdentificacionTests(SimpleTestCase):
    ceros = {'A': 0.4, 'B': 0.05, 'C': 0.05}

    def _recortes(self, con=0.6, sin=0.6):
        return {'sesgo_con_recorte': con, 'sesgo_sin_recorte': sin}

    def test_base_que_explica_la_mayor_parte_es_dominante(self):
        veredicto = diag.identificar_causa('A', 0.6, 0.45, self._recortes(), self.ceros, -0.4)
        self.assertTrue(veredicto['hipotesis']['a']['identificada'])
        self.assertAlmostEqual(veredicto['hipotesis']['a']['fraccion_explicada'], 0.75)
        self.assertFalse(veredicto['hipotesis']['b']['identificada'])
        # Muchos ceros, pero la media móvil subestima: (c) descartada.
        self.assertTrue(veredicto['hipotesis']['c']['muchos_ceros'])
        self.assertFalse(veredicto['hipotesis']['c']['identificada'])
        self.assertEqual(veredicto['dominante'], 'a')

    def test_base_de_signo_contrario_no_explica_el_sesgo(self):
        veredicto = diag.identificar_causa('A', 0.6, -0.3, self._recortes(), self.ceros, -0.4)
        self.assertFalse(veredicto['hipotesis']['a']['identificada'])
        self.assertIsNone(veredicto['dominante'])

    def test_recorte_que_explica_el_exceso(self):
        veredicto = diag.identificar_causa('A', 0.6, 0.1, self._recortes(0.6, 0.1), self.ceros, -0.4)
        self.assertTrue(veredicto['hipotesis']['b']['identificada'])
        self.assertEqual(veredicto['dominante'], 'b')


class EscaladoTests(SimpleTestCase):
    def test_niveles_del_dataset_externo(self):
        externo = pd.DataFrame({'serie_id': ['x', 'x', 'y', 'y', 'z'], 'cantidad': [10.0, 20, 40, 60, 100]})
        self.assertEqual(niveles_externos(externo), {'minimo': 15.0, 'referencia': 50.0})

    def test_solo_se_escalan_las_series_bajo_el_rango_externo(self):
        historial = pd.DataFrame({
            'serie_id': ['bajo', 'bajo', 'alto', 'alto', 'vacio'],
            'cantidad': [4.0, 6.0, 30.0, 40.0, 0.0],
        })
        factores = factores_escala(historial, nivel_minimo=12.0, nivel_referencia=50.0)
        self.assertEqual(factores, {'alto': 1.0, 'bajo': 10.0, 'vacio': 1.0})

        escalado = escalar(historial, factores)
        np.testing.assert_allclose(escalado['cantidad'], [40.0, 60.0, 30.0, 40.0, 0.0])
        pd.testing.assert_frame_equal(escalar(escalado, invertir(factores)), historial.astype({'cantidad': float}))

    def _validacion(self, interno, registro=None, escalado=None, ajustar=None):
        def registrar(train):
            if registro is not None:
                registro.append(train.copy())
            return ModeloMediaMovil()
        return ValidacionOrigenMovil(interno, ajustar=ajustar or registrar, escalado=escalado)

    def test_el_ajuste_solo_cambia_en_la_serie_escalada(self):
        """Las filas de entrenamiento de las series dentro del rango externo
        son idénticas con y sin escalado; las de accesorio (nivel 7) se
        multiplican por su factor."""
        interno = _interno()
        sin, con = [], []
        origen = pd.Timestamp('2026-08-20')
        self._validacion(interno, sin).modelo(origen)
        validacion = self._validacion(interno, con, EscaladoNivel(nivel_minimo=9.0, nivel_referencia=50.0))
        validacion.modelo(origen)
        sin, con = sin[0].set_index(['serie_id', 'fecha']), con[0].set_index(['serie_id', 'fecha'])

        factores = validacion.factores[origen]
        self.assertEqual(factores['esmalte'], 1.0)
        self.assertEqual(factores['latex'], 1.0)
        self.assertGreater(factores['accesorio'], 1.0)
        for serie in ['esmalte', 'latex']:
            pd.testing.assert_frame_equal(sin.loc[serie], con.loc[serie])
        np.testing.assert_allclose(
            con.loc['accesorio', 'cantidad'], sin.loc['accesorio', 'cantidad'] * factores['accesorio'],
        )

    def test_desescalar_devuelve_el_pronostico_a_la_escala_original(self):
        """Con un modelo homogéneo (media móvil), escalar y desescalar no
        cambia ningún pronóstico: las otras categorías siguen igual y la
        escalada vuelve a sus unidades."""
        interno = _interno()
        origen = pd.Timestamp('2026-08-20')
        sin = self._validacion(interno).pronosticar(origen, 7)
        con = self._validacion(interno, escalado=EscaladoNivel(9.0, 50.0)).pronosticar(origen, 7)
        pd.testing.assert_frame_equal(sin, con)

    def test_los_factores_solo_usan_los_dias_del_origen(self):
        interno = _interno()
        origen = pd.Timestamp('2026-08-20')
        alterado = interno.copy()
        alterado.loc[alterado['fecha'] > origen, 'cantidad'] = 10_000.0

        escala = EscaladoNivel(9.0, 50.0)
        original = self._validacion(interno, escalado=escala)
        con_futuro = self._validacion(alterado, escalado=escala)
        original.modelo(origen)
        con_futuro.modelo(origen)
        self.assertEqual(original.factores[origen], con_futuro.factores[origen])


class EscaladoSinFugaTests(SimpleTestCase):
    """El factor de escala de cada origen se calcula solo con sus días de
    ajuste: ningún día del tramo pronosticado entra en él."""

    class EscaladoRegistrado(EscaladoNivel):
        def __init__(self, *args):
            super().__init__(*args)
            self.vistos = []

        def __call__(self, historial):
            self.vistos.append(set(historial['fecha']))
            return super().__call__(historial)

    def test_ningun_dia_pronosticado_entra_en_el_factor_de_su_origen(self):
        escalado = self.EscaladoRegistrado(9.0, 50.0)
        validacion = ValidacionOrigenMovil(_interno(), ajustar=lambda train: ModeloMediaMovil(), escalado=escalado)
        _, diario = validacion.evaluar(7)

        origenes = validacion.origenes(7)
        # Un cálculo por origen (se reutiliza entre ajuste y pronóstico).
        self.assertEqual(len(escalado.vistos), len(origenes))
        for origen, fechas_factor in zip(origenes, escalado.vistos):
            pronosticadas = set(diario.loc[diario['origen'] == origen, 'fecha'])
            self.assertEqual(len(pronosticadas), 7)
            self.assertFalse(fechas_factor & pronosticadas)
            self.assertEqual(max(fechas_factor), origen)

    def test_una_fuga_en_el_factor_seria_detectable(self):
        """Poner 10.000 unidades en los días posteriores al origen haría que
        accesorio dejara de quedar bajo el mínimo (factor 1) si esos días
        entraran al factor. No cambia ni el factor ni ningún pronóstico."""
        interno = _interno()
        origen = pd.Timestamp('2026-08-20')
        alterado = interno.copy()
        alterado.loc[alterado['fecha'] > origen, 'cantidad'] = 10_000.0
        self.assertEqual(factores_escala(alterado, 9.0, 50.0)['accesorio'], 1.0)

        def correr(datos):
            validacion = ValidacionOrigenMovil(
                datos, ajustar=lambda train: entrenar_modelo(train, {**HIPERPARAMETROS_BASE, 'n_estimators': 5}),
                escalado=EscaladoNivel(9.0, 50.0),
            )
            return validacion.pronosticar(origen, 7), validacion.factores[origen]

        original, factores_original = correr(interno)
        con_futuro, factores_futuro = correr(alterado)
        self.assertEqual(factores_original, factores_futuro)
        self.assertGreater(factores_original['accesorio'], 1.0)
        pd.testing.assert_frame_equal(original[METODOS], con_futuro[METODOS])


class ComparacionPorOrigenTests(SimpleTestCase):
    def test_cuenta_origenes_que_mejoran_y_empeoran(self):
        o1, o2 = pd.Timestamp('2026-08-14'), pd.Timestamp('2026-08-15')
        antes = pd.DataFrame({
            'origen': [o1, o1, o2, o2], 'categoria': ['A', 'B', 'A', 'B'],
            'real': [10.0, 10.0, 10.0, 10.0], 'modelo': [15.0, 10.0, 12.0, 10.0],
        })
        despues = antes.assign(modelo=[11.0, 10.0, 13.0, 9.0])
        resultado = diag.comparar_por_origen(antes, despues)

        # Total: o1 de 5/20 a 1/20 (mejora); o2 de 2/20 a 4/20 (empeora).
        self.assertEqual(resultado['total'], {'origenes': 2, 'mejoran': 1, 'empeoran': 1})
        self.assertEqual(resultado['por_categoria']['A'], {'origenes': 2, 'mejoran': 1, 'empeoran': 1})
        self.assertEqual(resultado['por_categoria']['B'], {'origenes': 2, 'mejoran': 0, 'empeoran': 1})


class ComandoDiagnosticarSesgoTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.interno = _interno()
        with mock.patch('inventario.ml.entrenamiento.DIR_MODELOS', Path(self.tmp.name)):
            modelo = entrenar_modelo(construir_features(self.interno), {**HIPERPARAMETROS_BASE, 'n_estimators': 10})
            ruta = guardar_modelo(modelo, 'base.json')
        for categoria in ['accesorio', 'esmalte', 'latex']:
            Producto.objects.create(
                codigo=f'P-{categoria}', nombre=categoria, categoria=categoria,
                precio_venta='10.00', costo_compra='5.00', lead_time_dias=7,
            )
        ModeloEntrenado.objects.create(
            fase=ModeloEntrenado.Fase.BASE, hiperparametros=HIPERPARAMETROS_BASE,
            mae=1, rmse=1, smape=0.1, r2=0.9, ruta_archivo=ruta, activo=True,
        )
        ModeloEntrenado.objects.create(
            fase=ModeloEntrenado.Fase.AJUSTADO, nivel='categoria',
            hiperparametros={**HIPERPARAMETROS_AJUSTE, 'n_estimators': 3},
            mae=1, rmse=1, smape=0.1, r2=0.4, ruta_archivo=ruta, activo=True,
            origen_datos_internos=Origen.REAL, dias_entrenamiento=24, dias_prueba=7,
        )
        # Dataset externo chico: series de nivel 20 a 80.
        self.externo = pd.DataFrame({
            'fecha': list(pd.date_range('2015-01-01', periods=10)) * 3,
            'serie_id': ['1-1'] * 10 + ['1-2'] * 10 + ['1-3'] * 10,
            'cantidad': [20.0] * 10 + [50.0] * 10 + [80.0] * 10,
        })

    def _correr(self, veredicto=None):
        salida = StringIO()
        dir_reportes = Path(self.tmp.name) / 'diagnostico'
        parches = [
            mock.patch.object(diagnosticar_sesgo, 'leer_demanda_interna_por_categoria', return_value=self.interno),
            mock.patch.object(diagnosticar_sesgo, 'leer_datos_externos', return_value=self.externo),
            mock.patch.object(diagnosticar_sesgo, 'DIR_REPORTES', dir_reportes),
        ]
        if veredicto is not None:
            parches.append(mock.patch.object(diag, 'identificar_causa', return_value=veredicto))
        for parche in parches:
            parche.start()
            self.addCleanup(parche.stop)
        # El archivo solo tiene que existir: su lectura está simulada.
        archivo_externo = Path(self.tmp.name) / 'train.csv'
        archivo_externo.touch()
        call_command('diagnosticar_sesgo', '--archivo-externo', str(archivo_externo), stdout=salida)
        return salida.getvalue(), json.loads(next(dir_reportes.glob('*.json')).read_text())

    def _estado_modelos(self):
        return list(ModeloEntrenado.objects.order_by('pk').values_list(
            'pk', 'fase', 'activo', 'r2', 'ruta_archivo', 'hiperparametros', 'fecha_evaluacion',
        ))

    def test_informe_completo_sin_activar_ni_registrar_modelos(self):
        antes = self._estado_modelos()
        salida, reporte = self._correr()

        self.assertEqual(antes, self._estado_modelos())
        self.assertIn('1. Caracterización del sesgo', salida)
        self.assertIn('2. Hipótesis', salida)
        self.assertIn('3. Corrección', salida)
        self.assertIn('No se tocaron datos ni se activó ni registró ningún modelo', salida)
        self.assertEqual(reporte['categoria'], 'accesorio')
        self.assertEqual(len(reporte['horizontes']['ventana']['origenes']), 11)
        self.assertEqual(set(reporte['caracterizacion']['error_por_posicion']['accesorio']), set(map(str, range(1, 8))))
        self.assertEqual(set(reporte['hipotesis']), {'a', 'b', 'c', 'd', 'veredicto'})

    def test_con_a_dominante_evalua_el_escalado_y_reporta_las_tres_categorias(self):
        veredicto = {'hipotesis': {
            'a': {'identificada': True, 'fraccion_explicada': 0.8},
            'b': {'identificada': False, 'fraccion_explicada': 0.0},
            'c': {'identificada': False, 'muchos_ceros': True, 'media_movil_mismo_signo': False},
            'd': {'identificada': False, 'contrastable': False},
        }, 'dominante': 'a'}
        salida, reporte = self._correr(veredicto)

        correccion = reporte['correccion']
        self.assertEqual(correccion['niveles'], {'minimo': 20.0, 'referencia': 50.0})
        # Accesorio (nivel 7) queda bajo el mínimo externo; las demás no se tocan.
        self.assertEqual(correccion['factores_por_serie']['esmalte'], [1.0])
        self.assertNotEqual(correccion['factores_por_serie']['accesorio'], [1.0])
        for horizonte in ('diario', 'ventana'):
            seccion = correccion['horizontes'][horizonte]
            self.assertEqual(set(seccion['antes']['modelo']['categorias']), {'accesorio', 'esmalte', 'latex'})
            self.assertEqual(set(seccion['despues']), {*METODOS, 'dispersion', 'supera'})
        self.assertIn('antes → después de escalar', salida)
        self.assertIn('Comparación pareada por origen', salida)

    def test_sin_causa_identificada_no_corrige(self):
        veredicto = {'hipotesis': {
            'a': {'identificada': False, 'fraccion_explicada': 0.1},
            'b': {'identificada': False, 'fraccion_explicada': 0.0},
            'c': {'identificada': False, 'muchos_ceros': False, 'media_movil_mismo_signo': False},
            'd': {'identificada': False, 'contrastable': False},
        }, 'dominante': None}
        salida, reporte = self._correr(veredicto)
        self.assertIsNone(reporte['correccion'])
        self.assertIn('Sin causa identificada no se corrige nada', salida)
