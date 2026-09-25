"""Tests de la pantalla de recomendaciones: resumen por estado con costo de
las críticas, fecha de la última corrida vencida, y tarjeta con lo
esencial en una línea más el detalle auditable en "¿Por qué?"."""
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ..models import Producto, Recomendacion


class PantallaRecomendacionesTests(TestCase):
    def setUp(self):
        User.objects.create_superuser(username='admin1', password='pass12345')
        self.client.login(username='admin1', password='pass12345')
        self.fecha = timezone.now()

    def _crear(self, codigo, estado, cantidad, costo='10.00', fecha=None, **extra):
        producto = Producto.objects.create(
            codigo=codigo, nombre=f'Pintura {codigo}', precio_venta='20.00', costo_compra=costo, categoria='latex',
        )
        datos = dict(
            producto=producto, fecha_generacion=fecha or self.fecha, estado=estado,
            stock_actual_snapshot=3, demanda_predicha_periodo=30.0, desviacion_demanda=1.0,
            lead_time_usado=7, stock_seguridad=4.0, punto_reorden=11.0, cantidad_sugerida=cantidad,
            nivel_servicio_objetivo=0.95, explicacion='Texto largo con la nota del lead time.',
            dias_horizonte=30, demanda_categoria_periodo=300.0, participacion_usada=0.1,
            lead_time_es_real=False, pedidos_en_transito=0,
        )
        datos.update(extra)
        return Recomendacion.objects.create(**datos)

    def _get(self):
        return self.client.get(reverse('inventario:recomendaciones_lista'))

    def test_resumen_por_estado_y_costo_de_las_criticas(self):
        self._crear('PIN-1', 'critico', 31)             # 31 u. × S/ 10 = 310
        self._crear('PIN-2', 'critico', 5, costo='2.50')  # 5 u. × S/ 2,50 = 12,50
        self._crear('PIN-3', 'critico', 8, costo='0.00')  # sin costo: fuera del total
        self._crear('PIN-4', 'reponer', 4)                # no es crítica
        self._crear('PIN-5', 'normal', 0)

        respuesta = self._get()
        resumen = respuesta.context['resumen']

        self.assertEqual([e['total'] for e in resumen['estados']], [3, 1, 1, 0])
        self.assertEqual(str(resumen['costo_criticas']), '322.50')
        self.assertEqual(resumen['criticas_sin_costo'], 1)
        self.assertContains(respuesta, '3</strong> críticos')
        self.assertContains(respuesta, '0</strong> en exceso')

    def test_solo_cuenta_el_ultimo_lote(self):
        self._crear('PIN-VIEJO', 'critico', 10, fecha=self.fecha - timedelta(days=1))
        self._crear('PIN-NUEVO', 'normal', 0)
        self.assertEqual(self._get().context['resumen']['total_criticas'], 0)

    def test_tarjeta_con_lo_esencial_y_el_por_que(self):
        self._crear('PIN-1', 'critico', 31)
        html = self._get().content.decode()
        self.assertIn('Pedir <strong>31 u.</strong>', html)
        self.assertIn('Costo estimado <strong>S/ 310,00</strong>', html)
        self.assertIn('Cubre <strong>31 días</strong>', html)  # 31 u. / 1 u. por día
        self.assertIn('300,0 u. pronosticadas para Látex', html)
        self.assertIn('10,00%', html)
        # El texto largo del lead time ya no se muestra si hay datos del cálculo.
        self.assertNotIn('Texto largo con la nota del lead time.', html)

    def test_recomendacion_antigua_muestra_la_explicacion_original(self):
        self._crear('PIN-1', 'critico', 31, dias_horizonte=None, demanda_categoria_periodo=None, participacion_usada=None)
        self.assertContains(self._get(), 'Texto largo con la nota del lead time.')

    def test_corrida_de_mas_de_siete_dias_se_marca_en_rojo(self):
        self._crear('PIN-1', 'normal', 0, fecha=timezone.now() - timedelta(days=8))
        respuesta = self._get()
        self.assertTrue(respuesta.context['corrida_desactualizada'])
        self.assertContains(respuesta, 'text-danger fw-semibold')

    def test_corrida_reciente_no_se_marca(self):
        self._crear('PIN-1', 'normal', 0, fecha=timezone.now() - timedelta(days=7))
        respuesta = self._get()
        self.assertFalse(respuesta.context['corrida_desactualizada'])
        self.assertNotContains(respuesta, 'text-danger fw-semibold')
