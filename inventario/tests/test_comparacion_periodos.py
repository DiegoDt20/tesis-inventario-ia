"""Tests de la comparación de indicadores entre dos periodos (pretest y
postest): el servicio, la pantalla, la descarga en Excel y el comando."""
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO, StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from ..models import ConteoDetalle, ConteoFisico, CostoAlmacenamiento, Pedido, PedidoDetalle, Producto
from ..servicios.comparacion_periodos import comparar_periodos, libro_comparacion, meses_con_datos, rango_mes
from ..servicios.indicadores import calcular_coi, calcular_ei, calcular_ns

AGOSTO = rango_mes(date(2026, 8, 1))
SETIEMBRE = rango_mes(date(2026, 9, 1))


class _ConDosMeses(TestCase):
    """Agosto: EI 1 de 2, NS 1 de 2, CA 100 + PD 3 u. × 20 = 160.
    Setiembre: EI 2 de 2, NS 2 de 2, CA 80 + PD 0 = 80."""

    @classmethod
    def setUpTestData(cls):
        cls.p1 = Producto.objects.create(
            codigo='PIN-001', nombre='Látex blanco', presentacion='galón',
            precio_venta='50.00', costo_compra='30.00', stock_actual=10,
        )
        cls.p2 = Producto.objects.create(
            codigo='PIN-002', nombre='Esmalte negro', presentacion='galón',
            precio_venta='60.00', costo_compra='40.00', stock_actual=10,
        )
        for corte, fisicos in ((date(2026, 8, 31), (10, 7)), (date(2026, 9, 30), (10, 10))):
            conteo = ConteoFisico.objects.create(fecha_corte=corte, responsable='Tester', origen='real')
            for producto, fisico in zip((cls.p1, cls.p2), fisicos):
                ConteoDetalle.objects.create(conteo=conteo, producto=producto, stock_sistema=10, stock_fisico=fisico)

        cls._pedido(date(2026, 8, 10), cls.p1, 5, 5, True)
        cls._pedido(date(2026, 8, 12), cls.p2, 5, 2, False)
        cls._pedido(date(2026, 9, 10), cls.p1, 4, 4, True)
        cls._pedido(date(2026, 9, 12), cls.p2, 6, 6, True)

        CostoAlmacenamiento.objects.create(
            periodo_mes=date(2026, 8, 1), concepto='Alquiler', monto=Decimal('100.00'), origen='real',
        )
        CostoAlmacenamiento.objects.create(
            periodo_mes=date(2026, 9, 1), concepto='Alquiler', monto=Decimal('80.00'), origen='real',
        )

    @classmethod
    def _pedido(cls, dia, producto, solicitada, atendida, a_tiempo):
        pedido = Pedido.objects.create(
            fecha_solicitud=timezone.make_aware(datetime(dia.year, dia.month, dia.day, 10)),
            cliente='Cliente Reservado', origen='real',
        )
        PedidoDetalle.objects.create(
            pedido=pedido, producto=producto, cantidad_solicitada=solicitada,
            cantidad_atendida=atendida, atendido_a_tiempo=a_tiempo,
            motivo_no_atencion=None if a_tiempo else PedidoDetalle.MotivoNoAtencion.SIN_STOCK,
        )


class CompararPeriodosTests(_ConDosMeses):
    def test_meses_con_datos(self):
        self.assertEqual(meses_con_datos('real'), [date(2026, 8, 1), date(2026, 9, 1)])
        self.assertEqual(meses_con_datos('prueba'), [])

    def test_los_valores_son_los_de_las_funciones_de_indicadores(self):
        resultado = comparar_periodos(AGOSTO, SETIEMBRE, origen='real')
        filas = {f['clave']: f for f in resultado['filas']}

        for clave_periodo, rango in (('antes', AGOSTO), ('despues', SETIEMBRE)):
            self.assertEqual(filas['ei'][clave_periodo], calcular_ei(*rango, origen='real')['valor'])
            self.assertEqual(filas['ns'][clave_periodo], calcular_ns(*rango, origen='real')['valor'])
            coi = calcular_coi(*rango, origen='real')
            self.assertEqual(filas['coi'][clave_periodo], coi['valor'])
            self.assertEqual(filas['ca'][clave_periodo], coi['almacenamiento'])
            self.assertEqual(filas['pd'][clave_periodo], coi['desabastecimiento'])

    def test_diferencia_variacion_y_sentido_de_la_mejora(self):
        filas = {f['clave']: f for f in comparar_periodos(AGOSTO, SETIEMBRE, origen='real')['filas']}

        self.assertEqual(filas['ei']['antes'], 50.0)
        self.assertEqual(filas['ei']['despues'], 100.0)
        self.assertEqual(filas['ei']['diferencia'], 50.0)   # puntos porcentuales
        self.assertEqual(filas['ei']['variacion'], 100.0)   # relativa al valor de antes
        self.assertTrue(filas['ei']['mejora'])
        self.assertEqual(filas['ei']['detalle_antes'], '1 de 2')

        # El COI mejora cuando baja: 160 → 80.
        self.assertEqual(filas['coi']['antes'], Decimal('160.00'))
        self.assertEqual(filas['coi']['diferencia'], Decimal('-80.00'))
        self.assertEqual(filas['coi']['variacion'], -50.0)
        self.assertFalse(filas['coi']['sube'])
        self.assertTrue(filas['coi']['mejora'])
        self.assertEqual(filas['pd']['detalle_antes'], '1 líneas, 3 u.')

    def test_periodo_sin_datos_queda_sin_valor_no_en_cero(self):
        julio = rango_mes(date(2026, 7, 1))
        filas = {f['clave']: f for f in comparar_periodos(julio, AGOSTO, origen='real')['filas']}
        for clave in ('ei', 'ns', 'coi', 'ca', 'pd'):
            self.assertIsNone(filas[clave]['antes'])
            self.assertIsNone(filas[clave]['diferencia'])
            self.assertIsNone(filas[clave]['mejora'])

    def test_mismo_periodo_no_es_mejora_ni_empeora(self):
        filas = comparar_periodos(AGOSTO, AGOSTO, origen='real')['filas']
        for fila in filas:
            self.assertEqual(fila['diferencia'], 0)
            self.assertIsNone(fila['sube'])
            self.assertIsNone(fila['mejora'])


class LibroComparacionTests(_ConDosMeses):
    def _libro(self):
        salida = BytesIO()
        libro_comparacion(comparar_periodos(AGOSTO, SETIEMBRE, origen='real')).save(salida)
        salida.seek(0)
        return load_workbook(salida)

    def test_hojas_y_resumen(self):
        libro = self._libro()
        self.assertEqual(libro.sheetnames, ['Resumen', 'EI por producto', 'NS por línea', 'COI - CA', 'COI - PD'])
        resumen = {fila[0]: fila for fila in libro['Resumen'].iter_rows(min_row=2, max_row=6, values_only=True)}
        ei = resumen['Exactitud del inventario (EI)']
        self.assertEqual((ei[2], ei[3], ei[4], ei[6], ei[8]), (50, '1 de 2', 100, 50, 'Mejora'))
        coi = resumen['Costos operativos de inventario (COI)']
        self.assertEqual((coi[2], coi[4], coi[6], coi[8]), (160, 80, -80, 'Mejora'))

    def test_el_detalle_cuadra_con_el_resumen(self):
        libro = self._libro()
        ei = list(libro['EI por producto'].iter_rows(min_row=2, values_only=True))
        self.assertEqual(len(ei), 4)
        self.assertEqual([f[7] for f in ei if f[0] == 'Antes'], ['Sí', 'No'])

        ns = list(libro['NS por línea'].iter_rows(min_row=2, values_only=True))
        self.assertEqual(len(ns), 4)
        self.assertEqual(sum(1 for f in ns if f[0] == 'Antes' and f[8] == 'Sí'), 1)

        pd = list(libro['COI - PD'].iter_rows(min_row=2, values_only=True))
        self.assertEqual(len(pd), 1)
        self.assertEqual((pd[0][0], pd[0][6], pd[0][9], pd[0][10]), ('Antes', 3, 20, 60))
        ca = list(libro['COI - CA'].iter_rows(min_row=2, values_only=True))
        self.assertEqual(sum(f[3] for f in ca if f[0] == 'Antes') + pd[0][10], 160)

    def test_no_exporta_el_nombre_del_cliente(self):
        libro = self._libro()
        for hoja in libro:
            for fila in hoja.iter_rows(values_only=True):
                self.assertNotIn('Cliente Reservado', [str(c) for c in fila])


class PantallaComparacionTests(_ConDosMeses):
    def setUp(self):
        self.client.force_login(User.objects.create_user('ana', password='x'))
        self.url = reverse('inventario:comparacion')

    def test_exige_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_por_defecto_compara_los_dos_ultimos_meses(self):
        respuesta = self.client.get(self.url)
        self.assertEqual(respuesta.context['mes_antes'], date(2026, 8, 1))
        self.assertEqual(respuesta.context['mes_despues'], date(2026, 9, 1))
        self.assertContains(respuesta, '01/08/2026 – 31/08/2026')
        self.assertContains(respuesta, 'S/ 160,00')
        self.assertContains(respuesta, 'S/ 80,00')
        self.assertContains(respuesta, '+50,00 pp')

    def test_meses_elegidos_y_parametro_invalido(self):
        respuesta = self.client.get(self.url, {'antes': '2026-09', 'despues': '2026-08'})
        self.assertEqual(respuesta.context['mes_antes'], date(2026, 9, 1))
        respuesta = self.client.get(self.url, {'antes': 'xx', 'despues': '2026-13'})
        self.assertEqual(respuesta.context['mes_antes'], date(2026, 8, 1))
        self.assertEqual(respuesta.context['mes_despues'], date(2026, 9, 1))

    def test_un_solo_mes_no_tiene_con_que_comparar(self):
        respuesta = self.client.get(self.url, {'despues': '2026-08'})
        self.assertIsNone(respuesta.context['resultado'])
        self.assertContains(respuesta, 'Hace falta un mes anterior con datos')

    def test_sin_datos(self):
        respuesta = self.client.get(self.url, {'origen': 'prueba'})
        self.assertContains(respuesta, 'Todavía no hay datos cargados')

    def test_descarga_excel(self):
        respuesta = self.client.get(self.url, {'formato': 'xlsx'})
        self.assertEqual(
            respuesta['Content-Type'], 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        self.assertIn('comparacion_indicadores_2026-08_2026-09.xlsx', respuesta['Content-Disposition'])
        libro = load_workbook(BytesIO(respuesta.content))
        self.assertIn('Resumen', libro.sheetnames)


class ComandoCompararIndicadoresTests(_ConDosMeses):
    def test_imprime_la_tabla_y_no_escribe(self):
        salida = StringIO()
        call_command('comparar_indicadores', stdout=salida)
        texto = salida.getvalue()
        self.assertIn('2026-08 → 2026-09', texto)
        self.assertIn('+50.00 pp', texto)
        self.assertIn('-80.00', texto)
        self.assertEqual(PedidoDetalle.objects.count(), 4)

    def test_sin_mes_anterior_falla(self):
        with self.assertRaisesMessage(CommandError, 'No hay un mes con datos anterior a 2026-08'):
            call_command('comparar_indicadores', despues='2026-08', stdout=StringIO())

    def test_mes_invalido(self):
        with self.assertRaisesMessage(CommandError, 'Mes inválido "agosto"'):
            call_command('comparar_indicadores', antes='agosto', stdout=StringIO())
