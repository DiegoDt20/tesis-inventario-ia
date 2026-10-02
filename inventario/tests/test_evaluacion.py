"""Tests de la exactitud del pronóstico (inventario/ml/evaluacion.py), de
la validación de origen móvil (inventario/ml/walk_forward.py) y del comando
evaluar_predicciones.

Se verifican las definiciones con valores calculados a mano (WAPE,
exactitud, acierto dentro de tolerancia, R² intra-categoría, agregación por
ventana, dispersión entre orígenes), que ni el modelo ni las líneas base
vean días del tramo pronosticado, que el pronóstico de ventana se encadene
sin usar la demanda real intermedia, y que el comando no active ni registre
ningún modelo.
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
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from inventario.management.commands import evaluar_predicciones
from inventario.ml.entrenamiento import (
    HIPERPARAMETROS_AJUSTE,
    HIPERPARAMETROS_BASE,
    entrenar_modelo,
    guardar_modelo,
)
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
from inventario.ml.walk_forward import (
    METODOS,
    ValidacionOrigenMovil,
    agregar_por_ventana,
    calcular_origenes,
    dispersion_entre_origenes,
    horizonte_lead_time,
    pronostico_media_movil_7,
)
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
        # Lead time de 7 días en las tres categorías (ventana H = 7).
        for categoria in ['accesorio', 'esmalte', 'latex']:
            Producto.objects.create(
                codigo=f'P-{categoria}', nombre=categoria, categoria=categoria,
                precio_venta='10.00', costo_compra='5.00', lead_time_dias=7,
            )
        self.vigente = ModeloEntrenado.objects.create(
            fase=ModeloEntrenado.Fase.AJUSTADO, nivel='categoria',
            hiperparametros={**HIPERPARAMETROS_AJUSTE, 'n_estimators': 5},
            mae=1, rmse=1, smape=0.1, r2=0.4, ruta_archivo=ruta, activo=True,
            origen_datos_internos=Origen.REAL, dias_entrenamiento=24, dias_prueba=7,
        )
        self.inactivo = ModeloEntrenado.objects.create(
            fase=ModeloEntrenado.Fase.AJUSTADO, nivel='categoria', hiperparametros={},
            mae=1, rmse=1, smape=0.1, r2=0.3, ruta_archivo=ruta, activo=False,
        )

    def _correr(self, *argumentos, interno=None):
        salida = StringIO()
        dir_reportes = Path(self.tmp.name) / 'evaluacion'
        interno = self.interno if interno is None else interno
        with mock.patch.object(evaluar_predicciones, 'leer_demanda_interna_por_categoria', return_value=interno), \
                mock.patch.object(evaluar_predicciones, 'DIR_REPORTES', dir_reportes):
            call_command('evaluar_predicciones', *argumentos, stdout=salida)
        return salida.getvalue(), dir_reportes

    def _estado_modelos(self):
        return list(ModeloEntrenado.objects.order_by('pk').values_list(
            'pk', 'fase', 'activo', 'r2', 'ruta_archivo', 'hiperparametros', 'fecha_evaluacion',
        ))

    def test_no_activa_ni_registra_ningun_modelo(self):
        antes = self._estado_modelos()
        salida, dir_reportes = self._correr()

        self.assertEqual(antes, self._estado_modelos())
        self.assertIn('No se activó ni registró ningún modelo', salida)
        self.assertEqual(len(list(dir_reportes.glob('evaluacion_walkforward_*.json'))), 1)

    def test_walk_forward_por_defecto_con_los_dos_horizontes(self):
        salida, dir_reportes = self._correr()
        reporte = json.loads(next(dir_reportes.glob('*.json')).read_text())

        diario, ventana = reporte['horizontes']['diario'], reporte['horizontes']['ventana']
        # 31 días, 14 de ajuste mínimo: 31 - 1 - 14 + 1 = 17 y 31 - 7 - 14 + 1 = 11.
        self.assertEqual((diario['horizonte_dias'], diario['n_origenes']), (1, 17))
        self.assertEqual((ventana['horizonte_dias'], ventana['n_origenes']), (7, 11))
        self.assertEqual(ventana['origenes'][0], '2026-08-14')
        self.assertEqual(ventana['origenes'][-1], '2026-08-24')
        self.assertEqual(len(ventana['ventanas']), 11 * 3)
        self.assertEqual(set(ventana['dispersion_entre_origenes']), set(METODOS))
        self.assertEqual(reporte['lead_times_productos'], {'7.0': 3})
        self.assertIn('H = 7 días', salida)
        self.assertIn('Desv. WAPE', salida)

    def test_division_unica_reproduce_la_evaluacion_anterior_y_la_guarda_en_el_vigente(self):
        salida, dir_reportes = self._correr('--no-walk-forward', '--horizonte', 'diario')
        self.vigente.refresh_from_db()
        reporte = json.loads(next(dir_reportes.glob('evaluacion_division_unica_*.json')).read_text())
        diario = reporte['horizontes']['diario']

        self.assertIsNotNone(self.vigente.fecha_evaluacion)
        self.assertAlmostEqual(self.vigente.wape, diario['resultados']['modelo']['total']['wape'])
        self.assertAlmostEqual(self.vigente.exactitud, 1 - self.vigente.wape)
        self.assertEqual(set(self.vigente.r2_intra_categoria), {'accesorio', 'esmalte', 'latex'})
        self.assertEqual(set(self.vigente.acierto_tolerancia), {'accesorio', 'esmalte', 'latex'})
        self.assertEqual(self.vigente.mejor_linea_base, diario['mejor_linea_base'])
        self.assertEqual(reporte['dias_prueba'], 7)
        self.assertEqual(diario['fechas_prueba'], ['2026-08-25', '2026-08-31'])
        self.assertEqual(len(diario['pronosticos_diarios']), 21)
        self.assertIsNone(ModeloEntrenado.objects.get(pk=self.inactivo.pk).fecha_evaluacion)
        self.assertIn('R² global', salida)
        self.assertIn('Media móvil 7 días', salida)

    def test_historico_insuficiente_falla_con_mensaje_claro(self):
        # 18 días: alcanza para el horizonte diario (4 orígenes) pero no
        # para una ventana de 7 (harían falta 14 + 7 = 21).
        corto = self.interno[self.interno['fecha'] < '2026-08-19']
        antes = self._estado_modelos()
        with self.assertRaisesMessage(CommandError, 'No hay ningún origen evaluable con horizonte 7'):
            self._correr(interno=corto)
        self.assertEqual(antes, self._estado_modelos())

    def test_advierte_si_hay_menos_de_cinco_origenes(self):
        corto = self.interno[self.interno['fecha'] < '2026-08-19']
        salida, _ = self._correr('--horizonte', 'diario', interno=corto)
        self.assertIn('Solo 4 origen(es) evaluado(s), menos de 5', salida)


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


def _validacion_rapida(interno, registro=None):
    """ValidacionOrigenMovil con un ajuste chico (sin modelo base) que anota
    las fechas con que se entrenó cada origen."""
    def ajustar(train):
        if registro is not None:
            registro.append(set(train['fecha']))
        return entrenar_modelo(train, {**HIPERPARAMETROS_BASE, 'n_estimators': 5})
    return ValidacionOrigenMovil(interno, ajustar=ajustar)


class OrigenesTests(SimpleTestCase):
    def test_cantidad_de_origenes_segun_historico_minimo_y_horizonte(self):
        fechas = list(pd.date_range('2026-08-01', periods=31, freq='D'))
        semanal = calcular_origenes(fechas, horizonte=7, min_dias_ajuste=14)
        diario = calcular_origenes(fechas, horizonte=1, min_dias_ajuste=14)

        self.assertEqual(len(semanal), 31 - 7 - 14 + 1)
        self.assertEqual(len(diario), 31 - 1 - 14 + 1)
        # El primero deja 14 días de ajuste; el último, t + 7 = último día.
        self.assertEqual(semanal[0], pd.Timestamp('2026-08-14'))
        self.assertEqual(semanal[-1], pd.Timestamp('2026-08-24'))
        self.assertEqual(calcular_origenes(fechas[:20], horizonte=7, min_dias_ajuste=14), [])

    def test_validacion_usa_min_dias_entrenamiento_y_parte_por_fecha(self):
        validacion = ValidacionOrigenMovil(_interno(), ajustar=lambda train: None)
        self.assertEqual(len(validacion.origenes(7)), 11)
        historial = validacion.historial_hasta(validacion.origenes(7)[0])
        # Las tres categorías de cada día van juntas.
        self.assertTrue((historial.groupby('fecha')['serie_id'].nunique() == 3).all())


class SinFugaTests(SimpleTestCase):
    def test_ningun_dia_pronosticado_entra_en_el_ajuste_de_su_origen(self):
        registro = []
        validacion = _validacion_rapida(_interno(), registro)
        _, diario = validacion.evaluar(7)

        origenes = validacion.origenes(7)
        self.assertEqual(len(registro), len(origenes))
        for origen, fechas_ajuste in zip(origenes, registro):
            pronosticadas = set(diario.loc[diario['origen'] == origen, 'fecha'])
            self.assertEqual(len(pronosticadas), 7)
            self.assertFalse(fechas_ajuste & pronosticadas)
            self.assertEqual(max(fechas_ajuste), origen)

    def test_una_fuga_seria_detectable(self):
        """Poner 10.000 unidades en todos los días posteriores al origen no
        cambia ningún pronóstico de ese origen (modelo ni líneas base). Si
        algún día del tramo entrara al ajuste o a las variables, sí cambiaría."""
        interno = _interno()
        origen = pd.Timestamp('2026-08-20')
        alterado = interno.copy()
        alterado.loc[alterado['fecha'] > origen, 'cantidad'] = 10_000.0

        original = _validacion_rapida(interno).pronosticar(origen, 7)
        con_futuro = _validacion_rapida(alterado).pronosticar(origen, 7)
        pd.testing.assert_frame_equal(original[METODOS], con_futuro[METODOS])
        self.assertFalse(original['real'].equals(con_futuro['real']))


class VentanaTests(SimpleTestCase):
    def test_agregacion_por_ventana_con_valores_conocidos(self):
        o1, o2 = pd.Timestamp('2026-08-14'), pd.Timestamp('2026-08-15')
        diario = pd.DataFrame({
            'origen': [o1, o1, o1, o1, o2, o2],
            'fecha': pd.to_datetime(['2026-08-15', '2026-08-16', '2026-08-15', '2026-08-16',
                                     '2026-08-16', '2026-08-17']),
            'categoria': ['A', 'A', 'B', 'B', 'A', 'A'],
            'real': [3.0, 4.0, 10.0, 20.0, 4.0, 6.0],
            'modelo': [2.0, 2.5, 12.0, 18.0, 5.0, 5.0],
        })
        ventanas = agregar_por_ventana(diario).set_index(['origen', 'categoria'])

        self.assertEqual(ventanas.loc[(o1, 'A'), 'real'], 7.0)
        self.assertEqual(ventanas.loc[(o1, 'A'), 'modelo'], 4.5)
        self.assertEqual(ventanas.loc[(o1, 'B'), 'real'], 30.0)
        self.assertEqual(ventanas.loc[(o1, 'B'), 'modelo'], 30.0)
        self.assertEqual(ventanas.loc[(o2, 'A'), 'real'], 10.0)
        self.assertEqual(ventanas.loc[(o2, 'A'), 'dias'], 2)
        self.assertEqual(ventanas.loc[(o2, 'A'), 'fecha_fin'], pd.Timestamp('2026-08-17'))

    def test_wape_de_ventana_distinto_del_promedio_de_wape_diarios(self):
        # Día 1: |10-5| / 10 = 0.5; día 2: |10-15| / 10 = 0.5 → promedio 0.5.
        # Ventana: |20-20| / 20 = 0: los errores diarios se compensan.
        diario = pd.DataFrame({
            'origen': pd.Timestamp('2026-08-14'),
            'fecha': pd.to_datetime(['2026-08-15', '2026-08-16']),
            'categoria': 'A', 'real': [10.0, 10.0], 'modelo': [5.0, 15.0],
        })
        promedio_diario = np.mean([calcular_wape([r], [p]) for r, p in zip(diario['real'], diario['modelo'])])
        ventana = agregar_por_ventana(diario)

        self.assertAlmostEqual(promedio_diario, 0.5)
        self.assertAlmostEqual(calcular_wape(ventana['real'], ventana['modelo']), 0.0)

    def test_pronostico_encadenado_no_usa_la_demanda_real_intermedia(self):
        """Cambiar la demanda real de un día intermedio de la ventana cambia la
        suma real pero no la pronosticada (ni del modelo ni de las bases)."""
        interno = _interno()
        origen = pd.Timestamp('2026-08-20')
        alterado = interno.copy()
        alterado.loc[alterado['fecha'] == origen + pd.Timedelta(days=3), 'cantidad'] += 500.0

        original = agregar_por_ventana(_validacion_rapida(interno).pronosticar(origen, 7))
        con_intermedio = agregar_por_ventana(_validacion_rapida(alterado).pronosticar(origen, 7))
        pd.testing.assert_frame_equal(original[METODOS], con_intermedio[METODOS])
        np.testing.assert_allclose(con_intermedio['real'] - original['real'], [500.0] * 3)

    def test_media_movil_encadena_sus_propios_pronosticos(self):
        historial = pd.DataFrame({
            'fecha': pd.date_range('2026-08-01', periods=7, freq='D'),
            'serie_id': 'A', 'cantidad': [1.0, 2, 3, 4, 5, 6, 7],
        })
        fechas = list(pd.date_range('2026-08-08', periods=2, freq='D'))
        pronostico = pronostico_media_movil_7(historial, fechas)['prediccion']
        # Día 8: media de 1..7 = 4; día 9: media de 2..7 y el 4 pronosticado.
        np.testing.assert_allclose(pronostico, [4.0, (2 + 3 + 4 + 5 + 6 + 7 + 4) / 7])


class DispersionEntreOrigenesTests(SimpleTestCase):
    def test_se_calcula_sobre_los_origenes_y_no_sobre_las_filas(self):
        o1, o2 = pd.Timestamp('2026-08-14'), pd.Timestamp('2026-08-15')
        ventanas = pd.DataFrame({
            'origen': [o1, o1, o2, o2],
            'categoria': ['A', 'B', 'A', 'B'],
            'real': [10.0, 100.0, 10.0, 100.0],
            'modelo': [0.0, 100.0, 10.0, 50.0],
        })
        dispersion = dispersion_entre_origenes(ventanas, 'modelo')
        por_origen = [10 / 110, 50 / 110]
        por_fila = np.abs(ventanas['real'] - ventanas['modelo']) / ventanas['real']

        self.assertEqual(dispersion['n_origenes'], 2)
        self.assertAlmostEqual(dispersion['desviacion'], np.std(por_origen, ddof=1))
        self.assertAlmostEqual(dispersion['media'], np.mean(por_origen))
        self.assertNotAlmostEqual(dispersion['desviacion'], np.std(por_fila, ddof=1))
        self.assertAlmostEqual(dispersion['por_categoria']['A']['desviacion'], np.std([1.0, 0.0], ddof=1))

    def test_un_solo_origen_no_tiene_desviacion(self):
        ventanas = pd.DataFrame({
            'origen': [pd.Timestamp('2026-08-14')], 'categoria': ['A'], 'real': [10.0], 'modelo': [8.0],
        })
        self.assertIsNone(dispersion_entre_origenes(ventanas, 'modelo')['desviacion'])


class HorizonteLeadTimeTests(TestCase):
    def _producto(self, codigo, categoria, lead_time, activo=True):
        Producto.objects.create(
            codigo=codigo, nombre=codigo, categoria=categoria, precio_venta='10.00',
            costo_compra='5.00', lead_time_dias=lead_time, activo=activo,
        )

    def test_mediana_del_lead_time_del_motor_redondeada_hacia_arriba(self):
        self._producto('A', Categoria.LATEX, 7)
        self._producto('B', Categoria.LATEX, 10)
        self._producto('C', Categoria.TEMPLE, 30)  # categoría no evaluada
        self._producto('D', Categoria.LATEX, 30, activo=False)
        self.assertEqual(horizonte_lead_time(['latex']), (9, {7.0: 1, 10.0: 1}))

        self._producto('E', Categoria.ESMALTE, 7)
        self.assertEqual(horizonte_lead_time(['latex', 'esmalte'])[0], 7)

    def test_sin_productos_falla(self):
        with self.assertRaises(ValueError):
            horizonte_lead_time(['latex'])
