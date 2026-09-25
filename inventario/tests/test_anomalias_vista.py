"""Tests de la pantalla de anomalías: filtros, agrupación por conteo
físico, orden por diferencia y revisión en bloque con motivo."""
from datetime import date

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ..models import Anomalia, ConteoDetalle, ConteoFisico, Producto


class PantallaAnomaliasTests(TestCase):
    def setUp(self):
        User.objects.create_superuser(username='admin1', password='pass12345')
        self.client.login(username='admin1', password='pass12345')
        self.conteo_ago = ConteoFisico.objects.create(fecha_corte=date(2026, 8, 24), responsable='Ana')
        self.conteo_sep = ConteoFisico.objects.create(fecha_corte=date(2026, 9, 20), responsable='Luis')
        self.sobra = self._anomalia('PIN-001', self.conteo_ago, 17, 28, Anomalia.Severidad.MEDIA)
        self.falta = self._anomalia('PIN-002', self.conteo_ago, 20, 5, Anomalia.Severidad.MEDIA)
        self.alta = self._anomalia('PIN-003', self.conteo_sep, 5, 12, Anomalia.Severidad.ALTA)

    def _anomalia(self, codigo, conteo, sistema, fisico, severidad):
        producto = Producto.objects.create(codigo=codigo, nombre=f'Pintura {codigo}', precio_venta=10, costo_compra=5)
        detalle = ConteoDetalle.objects.create(conteo=conteo, producto=producto, stock_sistema=sistema, stock_fisico=fisico)
        return Anomalia.objects.create(
            producto=producto, conteo_detalle=detalle, fecha_deteccion=timezone.now(),
            tipo=Anomalia.Tipo.DIFERENCIA_INVENTARIO, severidad=severidad, score=0.5,
            valor_observado=detalle.diferencia, valor_esperado=0, descripcion=f'Conteo de {codigo}',
        )

    def _get(self, **params):
        return self.client.get(reverse('inventario:anomalias_lista'), params, HTTP_HX_REQUEST='true')

    def test_agrupa_por_conteo_con_total_y_el_mas_reciente_primero(self):
        respuesta = self._get()
        grupos = respuesta.context['grupos']
        self.assertEqual([g['conteo'] for g in grupos], [self.conteo_sep, self.conteo_ago])
        self.assertEqual([g['total'] for g in grupos], [1, 2])
        self.assertContains(respuesta, 'Conteo físico del 24/08/2026')
        self.assertContains(respuesta, '2 anomalías')

    def test_diferencia_con_signo_y_color(self):
        html = self._get().content.decode()
        self.assertIn('<span class="diferencia-sobra"', html)
        self.assertIn('+11</span>', html)
        self.assertIn('<span class="diferencia-falta"', html)
        self.assertIn('-15</span>', html)

    def test_ordenar_por_diferencia(self):
        def codigos(orden):
            grupo = self._get(orden=orden).context['grupos'][1]
            return [a.producto.codigo for a in grupo['anomalias']]
        self.assertEqual(codigos('diferencia'), ['PIN-002', 'PIN-001'])
        self.assertEqual(codigos('-diferencia'), ['PIN-001', 'PIN-002'])

    def test_filtros_por_severidad_y_estado(self):
        self.assertEqual(self._get(severidad='alta').context['total_filtradas'], 1)
        self.falta.revisada = True
        self.falta.save()
        self.assertEqual(self._get().context['total_filtradas'], 2)
        self.assertEqual(self._get(estado='revisadas').context['total_filtradas'], 1)
        self.assertEqual(self._get(estado='todas').context['total_filtradas'], 3)
        # Un valor desconocido no rompe la pantalla: se ignora.
        self.assertEqual(self._get(severidad='xx', tipo='yy').context['total_filtradas'], 2)

    def test_revision_en_bloque_con_motivo(self):
        respuesta = self.client.post(
            reverse('inventario:anomalias_marcar_revisadas'),
            {'ids': [self.sobra.pk, self.falta.pk], 'motivo': 'ingreso_no_registrado', 'estado': 'pendientes'},
            HTTP_HX_REQUEST='true',
        )
        self.assertEqual(respuesta.context['total_filtradas'], 1)
        self.assertContains(respuesta, 'Ingreso no registrado</strong> 2')
        self.assertEqual(
            Anomalia.objects.filter(revisada=True, motivo_revision='ingreso_no_registrado').count(), 2,
        )
        self.assertFalse(Anomalia.objects.get(pk=self.alta.pk).revisada)

    def test_revision_en_bloque_no_pisa_una_ya_revisada(self):
        Anomalia.objects.filter(pk=self.sobra.pk).update(revisada=True, motivo_revision='error_conteo')
        self.client.post(
            reverse('inventario:anomalias_marcar_revisadas'),
            {'ids': [self.sobra.pk, self.falta.pk], 'motivo': 'merma_no_anotada'},
        )
        self.assertEqual(Anomalia.objects.get(pk=self.sobra.pk).motivo_revision, 'error_conteo')
        self.assertEqual(Anomalia.objects.get(pk=self.falta.pk).motivo_revision, 'merma_no_anotada')

    def test_motivo_opcional_e_invalido_se_ignora(self):
        self.client.post(
            reverse('inventario:anomalia_marcar_revisada', args=[self.sobra.pk]), {'motivo': 'inventado'},
        )
        self.sobra.refresh_from_db()
        self.assertTrue(self.sobra.revisada)
        self.assertEqual(self.sobra.motivo_revision, '')

    def test_revision_en_bloque_sin_seleccion_redirige_con_aviso(self):
        respuesta = self.client.post(reverse('inventario:anomalias_marcar_revisadas'), {})
        self.assertRedirects(respuesta, reverse('inventario:anomalias_lista'))
        self.assertFalse(Anomalia.objects.filter(revisada=True).exists())
