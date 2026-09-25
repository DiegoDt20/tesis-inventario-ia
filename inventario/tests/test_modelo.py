"""Tests de la pantalla técnica del modelo de predicción: comparación de
los tres modelos con sus cuatro métricas y marca de modelos descartados en
el historial."""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from ..models import ModeloEntrenado
from ..views.modelo import _comparacion_tres_modelos


def _crear_ajustado(**extra):
    datos = {
        'fase': ModeloEntrenado.Fase.AJUSTADO, 'nivel': 'categoria', 'hiperparametros': {},
        'mae': 11.89, 'rmse': 13.93, 'smape': 0.61, 'r2': 0.40,
        'mae_linea_base': 14.90, 'rmse_linea_base': 18.06, 'smape_linea_base': 0.89, 'r2_linea_base': -0.01,
        'mae_solo_interno': 11.76, 'rmse_solo_interno': 14.69, 'smape_solo_interno': 0.60,
        'r2_solo_interno': 0.33, 'ruta_archivo': 'x.json', 'activo': True,
    }
    datos.update(extra)
    return ModeloEntrenado.objects.create(**datos)


class ComparacionTresModelosTests(TestCase):
    def test_marca_ganador_por_metrica(self):
        comparacion = _comparacion_tres_modelos(_crear_ajustado())
        self.assertEqual(comparacion['metricas'], ['MAE', 'RMSE', 'SMAPE', 'R²'])
        ganadores = [
            [f['nombre'] for f in comparacion['filas'] if f['celdas'][i]['gana']]
            for i in range(4)
        ]
        # MAE y SMAPE: gana el más bajo (solo interno); RMSE: el más bajo
        # (ajustado); R²: el más alto (ajustado).
        self.assertEqual(ganadores, [
            ['Solo datos internos'], ['Preentrenado + ajustado'],
            ['Solo datos internos'], ['Preentrenado + ajustado'],
        ])

    def test_metricas_faltantes_no_compiten(self):
        # Modelos ajustados anteriores solo guardaban el MAE de la comparación.
        modelo = _crear_ajustado(
            rmse_linea_base=None, rmse_solo_interno=None, smape_linea_base=None,
            smape_solo_interno=None, r2_linea_base=None, r2_solo_interno=None,
        )
        comparacion = _comparacion_tres_modelos(modelo)
        celdas_rmse = [f['celdas'][1] for f in comparacion['filas']]
        self.assertEqual([c['valor'] for c in celdas_rmse], [None, None, 13.93])
        self.assertFalse(any(c['gana'] for c in celdas_rmse))
        self.assertTrue(comparacion['filas'][1]['celdas'][0]['gana'])

    def test_sin_comparacion_devuelve_none(self):
        base = _crear_ajustado(fase=ModeloEntrenado.Fase.BASE, mae_linea_base=None)
        self.assertIsNone(_comparacion_tres_modelos(base))
        self.assertIsNone(_comparacion_tres_modelos(None))


class HistorialDescartadoTests(TestCase):
    def setUp(self):
        User.objects.create_user(username='u1', password='pass12345')
        self.client.login(username='u1', password='pass12345')

    def test_descartado_muestra_etiqueta_y_guion_en_smape(self):
        _crear_ajustado()
        _crear_ajustado(
            nivel='producto', activo=False, smape=2.2592883977538104e16,
            descartado=True, motivo_descarte='SMAPE desbordado',
        )
        respuesta = self.client.get(reverse('inventario:modelo_detalle'))
        html = respuesta.content.decode()
        self.assertEqual(respuesta.status_code, 200)
        self.assertIn('Descartado', html)
        self.assertNotIn('22592883977538104', html.replace('.', '').replace(',', ''))
        self.assertIn('bi-trophy-fill', html)
