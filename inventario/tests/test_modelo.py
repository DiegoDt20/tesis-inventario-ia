"""Tests de la pantalla técnica del modelo de predicción (/modelo/):
conjunto de medición de cada modelo (y su migración), etiquetas de los
modelos vigentes, lectura de los reportes guardados de evaluación y de
ablación (el más reciente, degradando si falta alguno), advertencias y
marca del enfoque por producto descartado. La evaluación y la ablación del
detalle solo se muestran con MODELO_MOSTRAR_EVALUACION (False por defecto):
los tests de su contenido la activan."""
import json
import re
import tempfile
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from ..models import ModeloEntrenado
from ..servicios import reportes_modelo

ADVERTENCIA_R2 = 'El R² global mezcla categorías de volúmenes muy distintos'
ADVERTENCIA_SIGNOS = 'los valores p indican consistencia, no significancia estadística'
ADVERTENCIA_PRODUCTO = 'Enfoque por producto, descartado por densidad insuficiente de demanda'


def _crear(**extra):
    datos = {
        'fase': ModeloEntrenado.Fase.AJUSTADO, 'nivel': 'categoria', 'hiperparametros': {},
        'mae': 11.89, 'rmse': 13.93, 'smape': 0.61, 'r2': 0.40, 'ruta_archivo': 'x.json', 'activo': True,
    }
    datos.update(extra)
    return ModeloEntrenado.objects.create(**datos)


def _metricas(exactitud):
    return {
        'dias': 11, 'demanda_real': 600.0, 'demanda_pronosticada': 610.0, 'mae': 17.5, 'rmse': 21.9,
        'wape': 1 - exactitud, 'exactitud': exactitud, 'acierto_tolerancia': 0.364, 'r2': -3.687,
    }


def _evaluacion(exactitud_modelo, modelo=6, exactitud_diario=None, por_categoria=None, ventanas=None):
    """Reporte mínimo de evaluar_predicciones (walk-forward) con la
    estructura real. por_categoria: {categoria: exactitud} del modelo en la
    ventana (por defecto, solo accesorio con la exactitud total)."""
    def resultado(exactitud, categorias=None):
        total = {**_metricas(exactitud), 'r2_global': 0.859, 'r2_por_categoria': {'accesorio': -3.687}}
        categorias = categorias or {'accesorio': exactitud}
        return {'categorias': {c: _metricas(e) for c, e in categorias.items()}, 'total': total,
                'tolerancia_relativa': 0.2}

    def dispersion(desviacion):
        return {'desviacion': desviacion, 'media': 0.2, 'por_categoria': {'accesorio': {'desviacion': desviacion}}}

    def horizonte(exactitud, dias, categorias=None):
        return {
        'horizonte_dias': dias, 'n_origenes': 11, 'origenes': ['2026-08-14', '2026-08-24'],
        'resultados': {
            'modelo': resultado(exactitud, categorias), 'media_categoria': resultado(0.809),
            'media_movil_7': resultado(0.772), 'ultimo_valor': resultado(0.496),
        },
        'dispersion_entre_origenes': {
            'modelo': dispersion(0.047), 'media_categoria': dispersion(0.04),
            'media_movil_7': dispersion(0.076), 'ultimo_valor': dispersion(0.273),
        },
        'comparacion_por_origen': {
            base: {'origenes': 11, 'gana': 9, 'pierde': 2, 'empates': 0, 'p_bilateral': 0.0654296875,
                   'p_unilateral': 0.0327}
            for base in ('media_categoria', 'media_movil_7', 'ultimo_valor')
        },
        'mejor_linea_base': 'media_categoria',
        'modelo_supera_por_wape': {'media_categoria': True, 'media_movil_7': True, 'ultimo_valor': True},
        }

    return {
        'fecha': '2026-10-04T04:16:40+00:00', 'origen': 'real', 'modelo_ajustado': modelo, 'modelo_base': 1,
        'categorias_sin_pronostico': {'base': 2},
        'horizontes': {
            'ventana': {**horizonte(exactitud_modelo, 7, por_categoria), 'ventanas': ventanas or []},
            'diario': horizonte(exactitud_modelo if exactitud_diario is None else exactitud_diario, 1),
        },
    }


def _ablacion(exactitud_c):
    def metodo(exactitud, sesgo):
        return {'exactitud': {'total': {'exactitud': exactitud, 'wape': 1 - exactitud}},
                'sesgo': {'accesorio': {'sesgo_relativo': sesgo}}, 'dispersion': {}}

    metodos = {
        'A': metodo(0.793, 0.484), 'A_escalado': metodo(0.815, -0.038), 'B': metodo(0.653, 1.712),
        'C': metodo(exactitud_c, 0.313), 'media_categoria': metodo(0.809, 0.146),
        'media_movil_7': metodo(0.772, -0.411), 'ultimo_valor': metodo(0.496, -0.192),
    }
    resumen = {'metodos': metodos, 'comparaciones': [
        {'configuracion': 'C', 'contra': 'A_escalado', 'mide': 'aporte del ajuste', 'gana': 7, 'origenes': 11,
         'p_bilateral': 0.548828125},
    ]}
    return {
        'fecha': '2026-10-04T05:10:44+00:00', 'precision': 'sin ajuste con datos de la empresa',
        'configuraciones': {'A': 'A. Solo externo', 'A_escalado': 'A. Con escalado', 'B': 'B. Solo interno',
                            'C': 'C. Externo + interno'},
        'horizontes': {'ventana': {'resumen': resumen}, 'diario': {'resumen': resumen}},
        'lectura': {'texto': ['1. ¿Funciona sin ajuste? NO: WAPE ventana 20.7%.']},
    }


class ConjuntoMedicionTests(TestCase):
    def test_save_lo_deduce_de_la_fase_si_no_se_indica(self):
        self.assertEqual(_crear(fase=ModeloEntrenado.Fase.BASE).conjunto_medicion, 'externo')
        self.assertEqual(_crear().conjunto_medicion, 'interno')
        explicito = _crear(fase=ModeloEntrenado.Fase.BASE, conjunto_medicion='interno')
        self.assertEqual(explicito.conjunto_medicion, 'interno')


class MigracionConjuntoMedicionTests(TransactionTestCase):
    antes = [('inventario', '0028_modeloentrenado_escalado')]
    despues = [('inventario', '0029_modeloentrenado_conjunto_medicion')]

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_rellena_los_registros_existentes_segun_la_fase_sin_tocar_metricas(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.antes)
        Modelo = executor.loader.project_state(self.antes).apps.get_model('inventario', 'ModeloEntrenado')
        comunes = {'hiperparametros': {}, 'mae': 1.0, 'rmse': 2.0, 'smape': 0.1, 'ruta_archivo': 'x.json'}
        base = Modelo.objects.create(fase='base', r2=0.9034, **comunes)
        ajustado = Modelo.objects.create(fase='ajustado', r2=0.3407, **comunes)

        executor = MigrationExecutor(connection)
        executor.migrate(self.despues)
        Modelo = executor.loader.project_state(self.despues).apps.get_model('inventario', 'ModeloEntrenado')
        self.assertEqual(Modelo.objects.get(pk=base.pk).conjunto_medicion, 'externo')
        self.assertEqual(Modelo.objects.get(pk=ajustado.pk).conjunto_medicion, 'interno')
        self.assertEqual(Modelo.objects.get(pk=base.pk).r2, 0.9034)
        self.assertEqual(Modelo.objects.get(pk=ajustado.pk).r2, 0.3407)


class _PantallaBase(TestCase):
    """Usuario logueado, directorios de reportes temporales y los dos
    modelos vigentes (base y en producción)."""

    def setUp(self):
        User.objects.create_user(username='u1', password='pass12345')
        self.client.login(username='u1', password='pass12345')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir_evaluacion = Path(self.tmp.name) / 'evaluacion'
        self.dir_ablacion = Path(self.tmp.name) / 'ablacion'
        for parche in (
            mock.patch.object(reportes_modelo, 'DIR_EVALUACION', self.dir_evaluacion),
            mock.patch.object(reportes_modelo, 'DIR_ABLACION', self.dir_ablacion),
        ):
            parche.start()
            self.addCleanup(parche.stop)
        self.base = _crear(fase=ModeloEntrenado.Fase.BASE, nivel='producto', r2=0.9034, mae=5.42)
        self.produccion = _crear(r2=0.3407)

    def _escribir(self, directorio, nombre, contenido):
        directorio.mkdir(parents=True, exist_ok=True)
        (directorio / nombre).write_text(json.dumps(contenido))

    def _html(self):
        respuesta = self.client.get(reverse('inventario:modelo_detalle'))
        self.assertEqual(respuesta.status_code, 200)
        return respuesta.content.decode()


@override_settings(MODELO_MOSTRAR_EVALUACION=True)
class PantallaModeloTests(_PantallaBase):
    def test_muestra_el_reporte_mas_reciente_de_cada_uno(self):
        self._escribir(self.dir_evaluacion, 'evaluacion_walkforward_20261002_060705.json',
                       _evaluacion(0.794, modelo=self.produccion.pk))
        self._escribir(self.dir_evaluacion, 'evaluacion_walkforward_20261004_041640.json',
                       _evaluacion(0.836, modelo=self.produccion.pk))
        # La división única no es la evaluación vigente, aunque sea más nueva.
        self._escribir(self.dir_evaluacion, 'evaluacion_division_unica_20261005_000000.json', {'roto': True})
        self._escribir(self.dir_ablacion, 'ablacion_20261004_050903.json', _ablacion(0.700))
        self._escribir(self.dir_ablacion, 'ablacion_20261004_051044.json', _ablacion(0.836))
        html = self._html()

        self.assertIn('evaluacion_walkforward_20261004_041640.json', html)
        self.assertIn('83,6\xa0%', html)
        self.assertNotIn('79,4\xa0%', html)
        self.assertIn('ablacion_20261004_051044.json', html)
        self.assertNotIn('70,0\xa0%', html)
        self.assertIn('9 de 11 orígenes', html)
        self.assertIn('0,065', html)
        self.assertIn('−3,8\xa0%', html)  # sesgo de accesorio con signo
        self.assertIn('¿Funciona sin ajuste? NO: WAPE ventana 20,7%.', html)  # sin "1. " y con coma decimal
        self.assertNotIn('Comparación de los tres modelos', html)

    def test_degrada_si_falta_la_evaluacion_o_la_ablacion(self):
        html = self._html()
        self.assertIn('No hay un reporte de evaluación', html)
        self.assertIn('No hay un reporte del estudio de ablación', html)

        self._escribir(self.dir_ablacion, 'ablacion_20261004_051044.json', _ablacion(0.836))
        html = self._html()
        self.assertIn('No hay un reporte de evaluación', html)
        self.assertIn('ablacion_20261004_051044.json', html)

        (self.dir_ablacion / 'ablacion_20261004_051044.json').unlink()
        self._escribir(self.dir_evaluacion, 'evaluacion_walkforward_20261004_041640.json',
                       _evaluacion(0.836, modelo=self.produccion.pk))
        html = self._html()
        self.assertIn('evaluacion_walkforward_20261004_041640.json', html)
        self.assertIn('No hay un reporte del estudio de ablación', html)

    def test_reporte_ilegible_o_de_otro_modelo(self):
        self._escribir(self.dir_ablacion, 'ablacion_20261004_051044.json', {'sin': 'estructura'})
        self._escribir(self.dir_evaluacion, 'evaluacion_walkforward_20261004_041640.json', _evaluacion(0.8, modelo=999))
        html = self._html()
        self.assertIn('No hay un reporte del estudio de ablación', html)
        self.assertIn('corresponde al modelo #999, no al que está en producción', html)

    def test_no_recalcula_al_cargar(self):
        self._escribir(self.dir_evaluacion, 'evaluacion_walkforward_20261004_041640.json',
                       _evaluacion(0.836, modelo=self.produccion.pk))
        with mock.patch('inventario.ml.walk_forward.ValidacionOrigenMovil.evaluar', side_effect=AssertionError), \
                mock.patch('inventario.ml.evaluacion.evaluar_exactitud', side_effect=AssertionError):
            self._html()

    def test_las_tres_advertencias_estan_en_la_pagina(self):
        _crear(nivel='producto', activo=False)
        self._escribir(self.dir_evaluacion, 'evaluacion_walkforward_20261004_041640.json',
                       _evaluacion(0.836, modelo=self.produccion.pk))
        self._escribir(self.dir_ablacion, 'ablacion_20261004_051044.json', _ablacion(0.836))
        html = self._html()
        self.assertIn(ADVERTENCIA_R2, html)
        self.assertIn('sobreestima el desempeño', html)
        self.assertIn('comparten 6 de sus 7 días pronosticados', html)
        self.assertIn(ADVERTENCIA_SIGNOS, html)
        self.assertIn(ADVERTENCIA_PRODUCTO, html)
        self.assertIn('comparable con el enfoque por categoría vigente', ' '.join(html.split()))

    def test_filas_de_nivel_producto_marcadas_como_enfoque_descartado(self):
        _crear(nivel='producto', activo=False, descartado=True, motivo_descarte='SMAPE desbordado',
               smape=2.2592883977538104e16)
        html = self._html()
        # Una sola fila ajustada de nivel producto: una sola marca. El base
        # (nivel producto por defecto, series de Kaggle) no se marca.
        self.assertEqual(html.count('enfoque-descartado'), 1)
        self.assertEqual(html.count(ADVERTENCIA_PRODUCTO), 1)
        self.assertIn('Series tienda-ítem de Kaggle', html)
        self.assertIn('Descartado', html)
        self.assertNotIn('22592883977538104', html.replace('.', '').replace(',', ''))

    def test_etiquetas_por_funcion_y_conjunto_de_medicion(self):
        _crear(activo=False)
        html = self._html()
        self.assertIn('Base vigente', html)
        self.assertIn('En producción', html)
        self.assertIn('Reemplazado', html)
        self.assertNotIn('> Activo<', html)
        # La fila del base dice en la propia fila que no es comparable.
        self.assertIn('Dataset externo (Kaggle)', html)
        self.assertIn('No comparable con las filas de la microempresa', html)
        self.assertIn('Datos de la microempresa', html)


class SemaforoTests(TestCase):
    def test_umbrales_fijos_verde_ambar_rojo(self):
        self.assertEqual(reportes_modelo.color_semaforo(0.868), 'verde')
        self.assertEqual(reportes_modelo.color_semaforo(0.80), 'verde')
        self.assertEqual(reportes_modelo.color_semaforo(0.7999), 'ambar')
        self.assertEqual(reportes_modelo.color_semaforo(0.672), 'ambar')
        self.assertEqual(reportes_modelo.color_semaforo(0.60), 'ambar')
        self.assertEqual(reportes_modelo.color_semaforo(0.5999), 'rojo')
        self.assertEqual(reportes_modelo.color_semaforo(-0.03), 'rojo')
        self.assertIsNone(reportes_modelo.color_semaforo(None))


class ResumenPantallaTests(_PantallaBase):
    """Resumen en castellano llano arriba de /modelo/ y detalle plegado."""

    CATEGORIAS = {'accesorio': 0.6724391260483478, 'esmalte': 0.8680076625133349, 'latex': 0.5}

    def _con_reporte(self):
        self._escribir(self.dir_evaluacion, 'evaluacion_walkforward_20261004_041640.json', _evaluacion(
            0.8359229837362032, modelo=self.produccion.pk, exactitud_diario=0.488, por_categoria=self.CATEGORIAS,
        ))

    def _resumen(self, html):
        return html[html.index('resumen-pronostico'):html.index('<details')]

    @override_settings(MODELO_MOSTRAR_EVALUACION=True)
    def test_mismo_valor_que_la_tabla_de_ventana_y_del_mismo_reporte(self):
        self._con_reporte()
        respuesta = self.client.get(reverse('inventario:modelo_detalle'))
        contexto, html = respuesta.context, respuesta.content.decode()
        total_ventana = next(f for f in contexto['evaluacion']['horizontes'][0]['filas'] if f['es_total'])

        self.assertEqual(contexto['evaluacion']['horizontes'][0]['clave'], 'ventana')
        self.assertEqual(contexto['resumen']['exactitud'], total_ventana['exactitud'])
        self.assertEqual(contexto['resumen']['exactitud'], 0.8359229837362032)
        self.assertIn('<div class="resumen-numero">83,6\xa0%</div>', html)
        # El mismo texto aparece en la tabla de la ventana, dentro del detalle.
        self.assertIn('83,6\xa0%', html[html.index('<details'):])
        # El contexto del lead time va en la ayuda emergente, no como frase.
        self.assertIn('data-bs-title="El sistema usa este pronóstico para calcular cuánto reponer durante los 7 '
                      'días que tarda en llegar un pedido al proveedor."', html)

    def test_colores_por_categoria_y_advertencia(self):
        self._con_reporte()
        resumen = self._resumen(self._html())
        self.assertIn('semaforo-ambar', resumen)  # accesorio, 67,2 %
        self.assertIn('semaforo-verde', resumen)  # esmalte, 86,8 %
        self.assertIn('semaforo-rojo', resumen)   # látex de prueba, 50 %
        self.assertIn('Accesorio', resumen)
        self.assertIn('Látex', resumen)
        self.assertIn('Accesorio y Látex: conviene revisar las sugerencias antes de comprar.', resumen)

    def test_advertencia_de_accesorios_con_los_valores_reales(self):
        self.CATEGORIAS = {'accesorio': 0.6724391260483478, 'esmalte': 0.8680076625133349, 'latex': 0.8367900583794133}
        self._con_reporte()
        resumen = self._resumen(self._html())
        self.assertIn('Accesorio: conviene revisar la sugerencia antes de comprar.', resumen)
        self.assertIn('bi-exclamation-triangle-fill', resumen)
        self.assertIn('alert-warning', resumen)

    def test_sin_jerga_ni_horizonte_diario_en_el_resumen(self):
        self._con_reporte()
        resumen = self._resumen(self._html())
        for jerga in ('WAPE', 'R²', 'valor p', 'p bilateral', 'orígenes', 'desviación', 'Desv.'):
            self.assertNotIn(jerga, resumen)
        self.assertNotIn('48,8', resumen)

    def test_detalle_tecnico_cerrado_por_defecto(self):
        self._con_reporte()
        html = self._html()
        self.assertIn('<details class="detalle-tecnico">', html)
        self.assertNotIn('<details class="detalle-tecnico" open>', html)
        self.assertIn('Detalle técnico de la investigación', html)
        # Los bloques visibles por defecto están adentro.
        detalle = html[html.index('<details'):html.index('</details>')]
        for texto in ('Modelo en producción', 'Modelo base', 'Historial de modelos entrenados'):
            self.assertIn(texto, detalle)
        # Moverse por las páginas del historial lo deja abierto.
        respuesta = self.client.get(reverse('inventario:modelo_detalle') + '?page=1')
        self.assertIn('<details class="detalle-tecnico" open>', respuesta.content.decode())

    def test_sin_reporte_muestra_el_aviso_y_el_comando(self):
        resumen = self._resumen(self._html())
        self.assertIn('Todavía no se generó la evaluación del pronóstico', resumen)
        self.assertIn('python manage.py evaluar_predicciones', resumen)
        self.assertNotIn('resumen-numero', resumen)
        self.assertNotIn('%', resumen)

    def test_reporte_de_otro_modelo_no_muestra_un_numero_viejo(self):
        self._escribir(self.dir_evaluacion, 'evaluacion_walkforward_20261004_041640.json',
                       _evaluacion(0.794, modelo=999))
        resumen = self._resumen(self._html())
        self.assertIn('La evaluación guardada es de un modelo anterior (#999)', resumen)
        self.assertNotIn('resumen-numero', resumen)
        self.assertNotIn('79,4', resumen)



class VisibilidadEvaluacionTests(_PantallaBase):
    """MODELO_MOSTRAR_EVALUACION oculta (False, por defecto) o muestra (True)
    la evaluación por horizonte, las líneas base y la ablación del detalle,
    sin tocar los tres bloques fijos ni el resumen de arriba."""

    # Textos propios de cada sección oculta: los dos horizontes, la
    # comparación con las líneas base y la ablación.
    OCULTABLES = (
        'Exactitud del pronóstico del modelo en producción', 'Ventana de lead time', 'Diario',
        'Contra las tres líneas base', 'Estudio de ablación', ADVERTENCIA_R2, ADVERTENCIA_SIGNOS,
    )
    FIJOS = ('Modelo en producción', 'Modelo base', 'Historial de modelos entrenados')

    def setUp(self):
        super().setUp()
        self._escribir(self.dir_evaluacion, 'evaluacion_walkforward_20261004_041640.json',
                       _evaluacion(0.836, modelo=self.produccion.pk, exactitud_diario=0.488))
        self._escribir(self.dir_ablacion, 'ablacion_20261004_051044.json', _ablacion(0.836))

    def _detalle(self):
        html = self._html()
        return html, html[html.index('<details'):html.index('</details>')]

    @override_settings(MODELO_MOSTRAR_EVALUACION=False)
    def test_con_false_las_secciones_no_aparecen(self):
        html, detalle = self._detalle()
        for texto in self.OCULTABLES:
            self.assertNotIn(texto, html)
        self.assertNotIn('ablacion_20261004_051044.json', html)
        self.assertNotIn('48,8', html)
        for texto in self.FIJOS:
            self.assertIn(texto, detalle)
        # El resumen de arriba sigue leyendo el mismo reporte.
        self.assertIn('<div class="resumen-numero">83,6\xa0%</div>', html)

    @override_settings(MODELO_MOSTRAR_EVALUACION=True)
    def test_con_true_las_secciones_aparecen(self):
        html, detalle = self._detalle()
        for texto in self.OCULTABLES:
            self.assertIn(texto, detalle)
        self.assertIn('ablacion_20261004_051044.json', detalle)
        self.assertIn('48,8\xa0%', detalle)
        for texto in self.FIJOS:
            self.assertIn(texto, detalle)
        self.assertIn('<div class="resumen-numero">83,6\xa0%</div>', html)


# Ventanas tal como las guarda evaluar_predicciones. La primera de accesorio
# es la verificada a mano con los pedidos reales: del 15/08 al 21/08 se
# pidieron 10 + 18 + 18 = 46 unidades; el pronóstico del origen 14/08 fue
# 71.2256000544488.
VENTANAS = [
    {'origen': '2026-08-14', 'categoria': 'accesorio', 'fecha_inicio': '2026-08-15', 'fecha_fin': '2026-08-21',
     'dias': 7, 'real': 46.0, 'modelo': 71.2256000544488, 'media_categoria': 60.0},
    {'origen': '2026-08-15', 'categoria': 'accesorio', 'fecha_inicio': '2026-08-16', 'fecha_fin': '2026-08-22',
     'dias': 7, 'real': 36.0, 'modelo': 64.5, 'media_categoria': 58.0},
    {'origen': '2026-08-14', 'categoria': 'esmalte', 'fecha_inicio': '2026-08-15', 'fecha_fin': '2026-08-21',
     'dias': 7, 'real': 345.0, 'modelo': 257.134729385376, 'media_categoria': 270.0},
]


class GraficoResumenTests(_PantallaBase):
    """Gráfico de pronóstico contra venta real del resumen y peso visual."""

    CATEGORIAS = {'accesorio': 0.6724391260483478, 'esmalte': 0.8680076625133349, 'latex': 0.8367900583794133}

    def _con_reporte(self, ventanas=VENTANAS):
        self._escribir(self.dir_evaluacion, 'evaluacion_walkforward_20261004_041640.json', _evaluacion(
            0.8359229837362032, modelo=self.produccion.pk, por_categoria=self.CATEGORIAS, ventanas=ventanas,
        ))

    def _datos_grafico(self, html):
        inicio = html.index('<script id="datos-grafico-ventanas" type="application/json">')
        contenido = html[html.index('>', inicio) + 1:html.index('</script>', inicio)]
        return json.loads(contenido)

    def test_el_grafico_se_alimenta_del_reporte_y_coincide_en_la_ventana_verificada(self):
        self._con_reporte()
        respuesta = self.client.get(reverse('inventario:modelo_detalle'))
        html = respuesta.content.decode()
        series = {s['clave']: s for s in self._datos_grafico(html)}

        self.assertEqual(series['accesorio']['puntos'][0],
                         {'fecha': '2026-08-15', 'real': 46.0, 'pronostico': 71.2256000544488})
        self.assertEqual(series['esmalte']['puntos'], [
            {'fecha': '2026-08-15', 'real': 345.0, 'pronostico': 257.134729385376},
        ])
        # Lo dibujado es exactamente lo del contexto, que sale del reporte.
        self.assertEqual(self._datos_grafico(html), json.loads(json.dumps(respuesta.context['resumen']['grafico'])))
        self.assertIn('id="grafico-ventanas-accesorio"', html)
        self.assertIn('Pronóstico contra venta real', html)
        self.assertIn('Cada punto: lo vendido en los 7 días que empiezan en esa fecha, real y pronosticado.', html)

    def test_no_inventa_ventanas_que_no_estan_en_el_reporte(self):
        self._con_reporte()
        html = self._html()
        series = {s['clave']: s for s in self._datos_grafico(html)}
        # Látex no tiene ninguna ventana en el reporte: no se dibuja.
        self.assertNotIn('latex', series)
        self.assertNotIn('id="grafico-ventanas-latex"', html)
        self.assertEqual(len(series['accesorio']['puntos']), 2)
        self.assertEqual(len(series['esmalte']['puntos']), 1)

    def test_reporte_sin_detalle_por_ventana_no_dibuja_un_grafico_vacio(self):
        self._con_reporte(ventanas=[])
        html = self._html()
        self.assertIn('resumen-numero', html)
        self.assertNotIn('datos-grafico-ventanas', html)
        self.assertNotIn('<canvas id="grafico-ventanas', html)

    def test_sin_reporte_muestra_el_aviso_y_ningun_grafico(self):
        html = self._html()
        self.assertIn('Todavía no se generó la evaluación del pronóstico', html)
        self.assertNotIn('datos-grafico-ventanas', html)
        self.assertNotIn('<canvas id="grafico-ventanas', html)

    def test_barras_con_el_color_y_el_valor_de_cada_categoria(self):
        self._con_reporte()
        html = self._html()
        self.assertIn('barra-categoria semaforo-ambar', html)
        self.assertIn('style="width: 67.2%;"', html)
        self.assertIn('style="width: 86.8%;"', html)
        self.assertIn('<div class="resumen-numero">83,6\xa0%</div>', html)

    def test_detalle_tecnico_sigue_cerrado_por_defecto(self):
        self._con_reporte()
        html = self._html()
        self.assertIn('<details class="detalle-tecnico">', html)
        self.assertNotIn('<details class="detalle-tecnico" open>', html)
        self.assertIn('Ver detalle', html)
        # El gráfico está en el resumen, antes del detalle, no adentro.
        self.assertLess(html.index('datos-grafico-ventanas'), html.index('<details'))

    def test_indicadores_sin_frases_explicativas(self):
        """Del resumen queda solo: la etiqueta, el total, las barras y la
        advertencia. Lead time y umbrales van detrás de un ícono de ayuda."""
        self._con_reporte()
        html = self._html()
        resumen = html[html.index('resumen-pronostico'):html.index('grafico-ventanas')]
        sin_scripts = re.sub(r'<script.*?</script>', ' ', resumen, flags=re.S)
        texto_visible = re.sub(r'<[^>]+>', ' ', sin_scripts)
        self.assertNotIn('De cada 100 unidades', html)
        self.assertNotIn('El sistema usa este pronóstico', texto_visible)
        self.assertNotIn('Verde desde 80', texto_visible)
        self.assertIn('data-bs-title="Verde desde 80 %, ámbar entre 60 % y 80 %, rojo por debajo de 60 %."', resumen)
        self.assertEqual(sin_scripts.count('data-bs-toggle="tooltip"'), 2)
        self.assertIn('Exactitud del pronóstico', texto_visible)

    def test_anillo_de_progreso_con_el_total(self):
        self._con_reporte()
        html = self._html()
        self.assertIn('class="anillo-progreso semaforo-verde"', html)
        self.assertIn('stroke-dasharray="83.6 100"', html)
        self.assertIn('aria-label="Exactitud del pronóstico: 83,6\xa0% de 100 %"', html)

    def test_barras_con_el_color_del_semaforo_en_los_tres_casos(self):
        self.CATEGORIAS = {'accesorio': 0.6724391260483478, 'esmalte': 0.8680076625133349, 'latex': 0.55}
        self._con_reporte()
        html = self._html()
        barras = re.findall(r'class="barra-categoria semaforo-(\w+)">\s*<span class="barra-categoria-nombre">(\w+)', html)
        self.assertEqual(barras, [('ambar', 'Accesorio'), ('verde', 'Esmalte'), ('rojo', 'Látex')])

