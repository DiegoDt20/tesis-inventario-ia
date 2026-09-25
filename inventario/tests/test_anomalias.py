"""Tests de los detectores de anomalías de control de existencias
(inventario/ml/anomalias.py)."""
from datetime import date, datetime
from io import StringIO

from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from inventario.ml.anomalias import detectar_diferencias_inventario, detectar_movimientos_atipicos
from inventario.models import Anomalia, ConteoFisico, ConteoDetalle, Movimiento, Producto


class DetectarDiferenciasInventarioTests(TestCase):
    def _crear_conteo_detalle(self, codigo, stock_sistema, stock_fisico):
        producto = Producto.objects.create(
            codigo=codigo, nombre=f'Producto {codigo}',
            precio_venta='10.00', costo_compra='5.00',
        )
        conteo = ConteoFisico.objects.create(fecha_corte=date(2026, 8, 24), responsable='Test')
        return ConteoDetalle.objects.create(
            conteo=conteo, producto=producto,
            stock_sistema=stock_sistema, stock_fisico=stock_fisico,
        )

    def test_diferencia_grande_se_marca_anomala(self):
        # 25% de diferencia: por encima del umbral del 20%.
        detalle = self._crear_conteo_detalle('PIN-100', stock_sistema=100, stock_fisico=75)

        hallazgos = detectar_diferencias_inventario()

        self.assertTrue(any(h.producto_id == detalle.producto_id for h in hallazgos))

    def test_diferencia_pequena_no_se_marca(self):
        # 2% de diferencia: dentro de lo normal.
        detalle = self._crear_conteo_detalle('PIN-101', stock_sistema=100, stock_fisico=98)

        hallazgos = detectar_diferencias_inventario()

        self.assertFalse(any(h.producto_id == detalle.producto_id for h in hallazgos))

    def test_sin_conteos_no_falla(self):
        self.assertEqual(detectar_diferencias_inventario(), [])

    def _severidad(self, codigo, stock_sistema, stock_fisico):
        detalle = self._crear_conteo_detalle(codigo, stock_sistema, stock_fisico)
        hallazgos = detectar_diferencias_inventario()
        return next(h for h in hallazgos if h.producto_id == detalle.producto_id)

    def test_faltante_total_es_alta(self):
        # Físico en 0: exactamente 100%, el máximo posible para un faltante.
        self.assertEqual(self._severidad('PIN-111', stock_sistema=50, stock_fisico=0).severidad, 'alta')

    def test_severidad_alta_desde_el_100_por_ciento(self):
        hallazgo = self._severidad('PIN-102', stock_sistema=10, stock_fisico=21)  # +110%
        self.assertEqual(hallazgo.severidad, 'alta')
        self.assertEqual(hallazgo.valor_observado, 11.0)
        self.assertEqual(hallazgo.valor_esperado, 0.0)

    def test_severidad_media_entre_40_y_100_por_ciento(self):
        self.assertEqual(self._severidad('PIN-103', 100, 40).severidad, 'media')   # -60%
        self.assertEqual(self._severidad('PIN-104', 100, 140).severidad, 'media')  # +40%, límite
        self.assertEqual(self._severidad('PIN-105', 100, 199).severidad, 'media')  # +99%

    def test_100_por_ciento_exacto_es_alta(self):
        self.assertEqual(self._severidad('PIN-112', 100, 200).severidad, 'alta')

    def test_severidad_baja_por_debajo_del_40_por_ciento(self):
        self.assertEqual(self._severidad('PIN-106', 100, 75).severidad, 'baja')  # -25%

    @override_settings(ANOMALIA_UMBRAL_ALTA=0.5, ANOMALIA_UMBRAL_MEDIA=0.2)
    def test_umbrales_configurables(self):
        self.assertEqual(self._severidad('PIN-107', 100, 40).severidad, 'alta')  # -60%

    @override_settings(ANOMALIA_UMBRAL_ALTA=0.4, ANOMALIA_UMBRAL_MEDIA=0.4)
    def test_umbrales_incoherentes_fallan(self):
        self._crear_conteo_detalle('PIN-108', 100, 40)
        with self.assertRaises(ImproperlyConfigured):
            detectar_diferencias_inventario()

    def test_descripcion_en_una_linea_sin_el_producto(self):
        hallazgo = self._severidad('PIN-109', stock_sistema=17, stock_fisico=28)
        self.assertEqual(hallazgo.descripcion, 'Conteo del 24/08: 28 físicas contra 17 registradas (+11, 64,7%)')

    def test_descripcion_sin_stock_en_sistema(self):
        hallazgo = self._severidad('PIN-110', stock_sistema=0, stock_fisico=5)
        self.assertEqual(hallazgo.descripcion, 'Conteo del 24/08: 5 físicas contra 0 registradas (+5, sin stock en sistema)')


class DetectarAnomaliasComandoTests(TestCase):
    def setUp(self):
        producto = Producto.objects.create(
            codigo='PIN-150', nombre='Producto', precio_venta='10.00', costo_compra='5.00',
        )
        conteo = ConteoFisico.objects.create(fecha_corte=date(2026, 8, 24), responsable='Test')
        self.detalle = ConteoDetalle.objects.create(
            conteo=conteo, producto=producto, stock_sistema=100, stock_fisico=40,
        )

    def _correr(self):
        call_command('detectar_anomalias', stdout=StringIO())

    def test_volver_a_correr_no_duplica_y_conserva_la_revision(self):
        self._correr()
        anomalia = Anomalia.objects.get()
        self.assertEqual(anomalia.conteo_detalle, self.detalle)
        self.assertEqual(anomalia.severidad, 'media')
        Anomalia.objects.update(revisada=True, motivo_revision='error_conteo')

        with override_settings(ANOMALIA_UMBRAL_ALTA=0.5, ANOMALIA_UMBRAL_MEDIA=0.2):
            self._correr()

        anomalia = Anomalia.objects.get()
        self.assertEqual(anomalia.severidad, 'alta')
        self.assertTrue(anomalia.revisada)
        self.assertEqual(anomalia.motivo_revision, 'error_conteo')


class DetectarMovimientosAtipicosTests(TestCase):
    def setUp(self):
        self.usuario = User.objects.create_user(username='tester', password='pass12345')
        self.producto = Producto.objects.create(
            codigo='PIN-200', nombre='Producto con historial',
            precio_venta='10.00', costo_compra='5.00', stock_actual=10_000,
        )

    def _crear_salida(self, cantidad, dia):
        return Movimiento.objects.create(
            producto=self.producto, tipo=Movimiento.Tipo.SALIDA,
            cantidad=cantidad,
            fecha=timezone.make_aware(datetime(2026, 8, dia)),
            usuario=self.usuario,
        )

    def test_producto_sin_historico_no_falla(self):
        hallazgos, sin_historico = detectar_movimientos_atipicos()

        self.assertEqual(hallazgos, [])
        self.assertEqual(sin_historico, [])

    def test_menos_de_5_movimientos_no_calcula_z_score(self):
        for dia in range(1, 4):  # solo 3 movimientos
            self._crear_salida(10, dia)

        hallazgos, sin_historico = detectar_movimientos_atipicos()

        self.assertEqual(hallazgos, [])
        self.assertEqual(sin_historico, [(self.producto.pk, Movimiento.Tipo.SALIDA, 3)])

    def test_movimiento_atipico_se_detecta_con_historico_suficiente(self):
        cantidades_habituales = [8, 9, 10, 11, 10, 12, 9, 10, 11, 10]
        for dia, cantidad in enumerate(cantidades_habituales, start=1):
            self._crear_salida(cantidad, dia)
        self._crear_salida(300, 20)  # muy por encima de lo habitual

        hallazgos, sin_historico = detectar_movimientos_atipicos()

        self.assertEqual(sin_historico, [])
        self.assertEqual(len(hallazgos), 1)
        self.assertEqual(hallazgos[0].producto_id, self.producto.pk)
        self.assertEqual(hallazgos[0].valor_observado, 300.0)
        self.assertTrue(hallazgos[0].descripcion.startswith('Salida del 20/08: 300 u. contra un promedio de'))

    def test_ajustes_no_se_evaluan(self):
        # Los ajustes no representan una cantidad vendida/comprada
        # comparable, así que ni cuentan como histórico ni se evalúan.
        for dia in range(1, 7):
            Movimiento.objects.create(
                producto=self.producto, tipo=Movimiento.Tipo.AJUSTE,
                cantidad=dia * 100, fecha=timezone.make_aware(datetime(2026, 8, dia)),
                usuario=self.usuario,
            )

        hallazgos, sin_historico = detectar_movimientos_atipicos()

        self.assertEqual(hallazgos, [])
        self.assertEqual(sin_historico, [])
