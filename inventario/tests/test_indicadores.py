"""Tests de inventario.servicios.indicadores: EI, NS y COI con datos conocidos.

Estas son las mismas funciones que usan el dashboard y el comando
cargar_datos, así que verificarlas aquí cubre ambos usos.
"""
from datetime import date, datetime
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from inventario.servicios.indicadores import calcular_coi, calcular_ei, calcular_ns, rango_disponible, serie_mensual
from inventario.models import (
    ConteoDetalle,
    ConteoFisico,
    CostoAlmacenamiento,
    Pedido,
    PedidoDetalle,
    Producto,
)


class CalcularEITests(TestCase):
    def setUp(self):
        self.producto = Producto.objects.create(
            codigo='PIN-001', nombre='Pintura A', precio_venta='50.00', costo_compra='30.00',
        )

    def test_ei_con_datos_conocidos(self):
        conteo = ConteoFisico.objects.create(fecha_corte=date(2026, 1, 15), responsable='Auditor')
        # 3 correctos (diferencia 0), 1 incorrecto -> EI = 3/4 * 100 = 75%
        ConteoDetalle.objects.create(conteo=conteo, producto=self.producto, stock_sistema=10, stock_fisico=10)
        ConteoDetalle.objects.create(conteo=conteo, producto=self.producto, stock_sistema=5, stock_fisico=5)
        ConteoDetalle.objects.create(conteo=conteo, producto=self.producto, stock_sistema=8, stock_fisico=8)
        ConteoDetalle.objects.create(conteo=conteo, producto=self.producto, stock_sistema=8, stock_fisico=3)

        resultado = calcular_ei(date(2026, 1, 1), date(2026, 1, 31))

        self.assertEqual(resultado['total'], 4)
        self.assertEqual(resultado['correctos'], 3)
        self.assertEqual(resultado['valor'], 75.0)

    def test_ei_fuera_de_rango_no_se_cuenta(self):
        conteo = ConteoFisico.objects.create(fecha_corte=date(2026, 3, 1), responsable='Auditor')
        ConteoDetalle.objects.create(conteo=conteo, producto=self.producto, stock_sistema=10, stock_fisico=1)

        resultado = calcular_ei(date(2026, 1, 1), date(2026, 1, 31))

        self.assertEqual(resultado['total'], 0)
        self.assertIsNone(resultado['valor'])


class CalcularNSTests(TestCase):
    def setUp(self):
        self.producto = Producto.objects.create(
            codigo='PIN-002', nombre='Pintura B', precio_venta='50.00', costo_compra='30.00',
        )

    def _crear_pedido_detalle(self, dia, solicitada, atendida, a_tiempo):
        pedido = Pedido.objects.create(
            fecha_solicitud=timezone.make_aware(datetime(2026, 1, dia)),
            cliente='C-001',
            canal=Pedido.Canal.MOSTRADOR,
        )
        return PedidoDetalle.objects.create(
            pedido=pedido, producto=self.producto,
            cantidad_solicitada=solicitada, cantidad_atendida=atendida,
            atendido_a_tiempo=a_tiempo,
        )

    def test_ns_con_datos_conocidos(self):
        # 2 a tiempo de 3 pedidos -> NS = 2/3 * 100 = 66.666...%
        self._crear_pedido_detalle(5, 10, 10, True)
        self._crear_pedido_detalle(10, 5, 5, True)
        self._crear_pedido_detalle(15, 8, 3, False)

        resultado = calcular_ns(date(2026, 1, 1), date(2026, 1, 31))

        self.assertEqual(resultado['total'], 3)
        self.assertEqual(resultado['a_tiempo'], 2)
        self.assertAlmostEqual(resultado['valor'], 66.6666, places=3)

    def test_ns_respeta_el_rango_de_fechas(self):
        self._crear_pedido_detalle(5, 10, 10, True)   # dentro del rango
        self._crear_pedido_detalle(20, 10, 0, False)  # fuera del rango (febrero)
        self._crear_pedido_detalle(1, 10, 10, True)

        resultado = calcular_ns(date(2026, 1, 1), date(2026, 1, 10))

        self.assertEqual(resultado['total'], 2)
        self.assertEqual(resultado['a_tiempo'], 2)
        self.assertEqual(resultado['valor'], 100.0)


class CalcularCOITests(TestCase):
    def setUp(self):
        # Margen unitario = precio_venta - costo_compra = 20.00
        self.producto = Producto.objects.create(
            codigo='PIN-003', nombre='Pintura C', precio_venta='50.00', costo_compra='30.00',
        )

    def test_coi_con_datos_conocidos(self):
        CostoAlmacenamiento.objects.create(
            periodo_mes=date(2026, 1, 1), concepto='Alquiler', monto=Decimal('500.00'),
        )
        # Fuera de rango: no debe sumarse.
        CostoAlmacenamiento.objects.create(
            periodo_mes=date(2026, 2, 1), concepto='Alquiler', monto=Decimal('999.00'),
        )

        pedido = Pedido.objects.create(
            fecha_solicitud=timezone.make_aware(datetime(2026, 1, 10)),
            cliente='C-001', canal=Pedido.Canal.MOSTRADOR,
        )
        # No atendido por completo: 10 - 4 = 6 unidades * margen 20.00 = 120.00
        PedidoDetalle.objects.create(
            pedido=pedido, producto=self.producto,
            cantidad_solicitada=10, cantidad_atendida=4, atendido_a_tiempo=False,
        )
        # Atendido por completo: no genera pérdida.
        PedidoDetalle.objects.create(
            pedido=pedido, producto=self.producto,
            cantidad_solicitada=5, cantidad_atendida=5, atendido_a_tiempo=True,
        )

        resultado = calcular_coi(date(2026, 1, 1), date(2026, 1, 31))

        self.assertEqual(resultado['almacenamiento'], Decimal('500.00'))
        self.assertEqual(resultado['desabastecimiento'], Decimal('120.00'))
        self.assertEqual(resultado['valor'], Decimal('620.00'))

    def test_coi_sin_datos_da_cero_no_none(self):
        resultado = calcular_coi(date(2026, 1, 1), date(2026, 1, 31))
        self.assertEqual(resultado['valor'], Decimal('0.00'))
        self.assertFalse(resultado['tiene_datos'])

    def test_coi_con_datos_marca_tiene_datos(self):
        CostoAlmacenamiento.objects.create(
            periodo_mes=date(2026, 1, 1), concepto='Alquiler', monto=Decimal('100.00'),
        )
        resultado = calcular_coi(date(2026, 1, 1), date(2026, 1, 31))
        self.assertTrue(resultado['tiene_datos'])


class SerieMensualTests(TestCase):
    def setUp(self):
        self.producto = Producto.objects.create(
            codigo='PIN-004', nombre='Pintura D', precio_venta='50.00', costo_compra='30.00',
        )

    def test_meses_sin_datos_quedan_en_none_no_en_cero(self):
        # Solo enero tiene datos; febrero debe quedar en None en los tres
        # indicadores, no en 0 (0 se leería como "midió cero", no "no midió").
        conteo = ConteoFisico.objects.create(fecha_corte=date(2026, 1, 10), responsable='Auditor')
        ConteoDetalle.objects.create(conteo=conteo, producto=self.producto, stock_sistema=5, stock_fisico=5)

        pedido = Pedido.objects.create(
            fecha_solicitud=timezone.make_aware(datetime(2026, 1, 10)),
            cliente='C-001', canal=Pedido.Canal.MOSTRADOR,
        )
        PedidoDetalle.objects.create(
            pedido=pedido, producto=self.producto,
            cantidad_solicitada=5, cantidad_atendida=5, atendido_a_tiempo=True,
        )
        CostoAlmacenamiento.objects.create(
            periodo_mes=date(2026, 1, 1), concepto='Alquiler', monto=Decimal('100.00'),
        )

        puntos = serie_mensual(date(2026, 1, 1), date(2026, 2, 28))
        por_periodo = {p['periodo']: p for p in puntos}

        self.assertIsNotNone(por_periodo['2026-01']['ei'])
        self.assertIsNotNone(por_periodo['2026-01']['ns'])
        self.assertIsNotNone(por_periodo['2026-01']['coi'])

        self.assertIsNone(por_periodo['2026-02']['ei'])
        self.assertIsNone(por_periodo['2026-02']['ns'])
        self.assertIsNone(por_periodo['2026-02']['coi'])


class RangoDisponibleTests(TestCase):
    def test_sin_datos_devuelve_none(self):
        self.assertEqual(rango_disponible(), (None, None))

    def test_cubre_el_rango_completo_de_todas_las_fuentes(self):
        producto = Producto.objects.create(
            codigo='PIN-005', nombre='Pintura E', precio_venta='50.00', costo_compra='30.00',
        )
        # El dato más antiguo viene de ConteoFisico, el más reciente de Pedido.
        ConteoFisico.objects.create(fecha_corte=date(2026, 1, 1), responsable='Auditor')
        Pedido.objects.create(
            fecha_solicitud=timezone.make_aware(datetime(2026, 6, 15)),
            cliente='C-001', canal=Pedido.Canal.MOSTRADOR,
        )
        CostoAlmacenamiento.objects.create(
            periodo_mes=date(2026, 3, 1), concepto='Alquiler', monto=Decimal('100.00'),
        )

        minimo, maximo = rango_disponible()

        self.assertEqual(minimo, date(2026, 1, 1))
        self.assertEqual(maximo, date(2026, 6, 15))
