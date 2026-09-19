"""Tests de los detectores de anomalías de control de existencias
(inventario/ml/anomalias.py)."""
from datetime import date, datetime

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from inventario.ml.anomalias import detectar_diferencias_inventario, detectar_movimientos_atipicos
from inventario.models import ConteoFisico, ConteoDetalle, Movimiento, Producto


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

    def test_severidad_alta_cuando_la_diferencia_es_la_mitad_o_mas(self):
        detalle = self._crear_conteo_detalle('PIN-102', stock_sistema=100, stock_fisico=40)

        hallazgos = detectar_diferencias_inventario()

        hallazgo = next(h for h in hallazgos if h.producto_id == detalle.producto_id)
        self.assertEqual(hallazgo.severidad, 'alta')
        self.assertEqual(hallazgo.valor_observado, -60.0)
        self.assertEqual(hallazgo.valor_esperado, 0.0)


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
