"""Tests de las pantallas de operación diaria (pedidos, movimientos,
conteos) y de los permisos de los grupos "administrador" y "operador"."""
from datetime import date

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from ..models import ConteoDetalle, ConteoFisico, Movimiento, Pedido, PedidoDetalle, Producto


def _crear_operador(username='operador1'):
    call_command('crear_grupos_permisos')
    usuario = User.objects.create_user(username=username, password='pass12345')
    usuario.groups.add(Group.objects.get(name='operador'))
    return usuario


class OperadorRegistraPedidoTests(TestCase):
    def setUp(self):
        self.usuario = _crear_operador()
        self.producto = Producto.objects.create(
            codigo='PIN-900', nombre='Latex Blanco', presentacion='galón',
            precio_venta='50.00', costo_compra='30.00', stock_actual=100,
        )
        self.client.login(username='operador1', password='pass12345')

    def _datos_formset(self, **extra):
        datos = {
            'fecha_solicitud': '2026-09-01',
            'cliente': 'Cliente de prueba',
            'canal': Pedido.Canal.MOSTRADOR,
            'form-TOTAL_FORMS': '1',
            'form-INITIAL_FORMS': '0',
            'form-MIN_NUM_FORMS': '0',
            'form-MAX_NUM_FORMS': '1000',
            'form-0-producto': f'{self.producto.codigo} — {self.producto.nombre} {self.producto.presentacion}',
            'form-0-cantidad_solicitada': '5',
            'form-0-cantidad_atendida': '5',
            'form-0-fecha_requerida': '',
            'form-0-motivo_no_atencion': '',
        }
        datos.update(extra)
        return datos

    def test_operador_puede_registrar_un_pedido(self):
        respuesta = self.client.post(
            reverse('inventario:pedido_nuevo'), self._datos_formset(guardar='1'),
        )

        self.assertEqual(Pedido.objects.count(), 1)
        pedido = Pedido.objects.get()
        self.assertEqual(pedido.cliente, 'Cliente de prueba')
        self.assertEqual(pedido.estado, Pedido.Estado.ATENDIDO)
        self.assertEqual(PedidoDetalle.objects.count(), 1)
        detalle = PedidoDetalle.objects.get()
        self.assertEqual(detalle.producto, self.producto)
        self.assertEqual(detalle.cantidad_atendida, 5)
        self.assertRedirects(respuesta, reverse('inventario:pedido_lista'))

    def test_pedido_con_atencion_parcial_exige_motivo(self):
        datos = self._datos_formset(guardar='1')
        datos['form-0-cantidad_atendida'] = '2'  # menor a la solicitada (5)
        # motivo_no_atencion queda vacío a propósito.

        self.client.post(reverse('inventario:pedido_nuevo'), datos)

        self.assertEqual(Pedido.objects.count(), 0)

    def test_pedido_con_atencion_parcial_y_motivo_se_guarda_pendiente(self):
        datos = self._datos_formset(guardar='1')
        datos['form-0-cantidad_atendida'] = '2'
        datos['form-0-motivo_no_atencion'] = PedidoDetalle.MotivoNoAtencion.SIN_STOCK

        self.client.post(reverse('inventario:pedido_nuevo'), datos)

        self.assertEqual(Pedido.objects.count(), 1)
        pedido = Pedido.objects.get()
        self.assertEqual(pedido.estado, Pedido.Estado.PENDIENTE)
        detalle = PedidoDetalle.objects.get()
        self.assertEqual(detalle.motivo_no_atencion, PedidoDetalle.MotivoNoAtencion.SIN_STOCK)
        self.assertFalse(detalle.atendido_a_tiempo)


class OperadorNoAccedeAlAdminTests(TestCase):
    def setUp(self):
        self.usuario = _crear_operador()

    def test_operador_no_puede_entrar_al_admin(self):
        self.client.login(username='operador1', password='pass12345')

        respuesta = self.client.get('/admin/')

        # No es staff: el admin de Django lo redirige a su login en vez de
        # mostrarle el índice.
        self.assertFalse(self.usuario.is_staff)
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn('/admin/login/', respuesta.url)

    def test_administrador_con_is_staff_si_puede_entrar(self):
        admin_group = Group.objects.get(name='administrador')
        administrador = User.objects.create_user(
            username='admin1', password='pass12345', is_staff=True,
        )
        administrador.groups.add(admin_group)
        self.client.login(username='admin1', password='pass12345')

        respuesta = self.client.get('/admin/')

        self.assertEqual(respuesta.status_code, 200)


class MovimientoStockNegativoTests(TestCase):
    def setUp(self):
        self.usuario = _crear_operador()
        self.producto = Producto.objects.create(
            codigo='PIN-901', nombre='Esmalte Rojo',
            precio_venta='40.00', costo_compra='25.00', stock_actual=5,
        )
        self.client.login(username='operador1', password='pass12345')

    def test_salida_mayor_al_stock_muestra_error_y_no_crea_el_movimiento(self):
        respuesta = self.client.post(reverse('inventario:movimiento_nuevo'), {
            'producto': f'{self.producto.codigo} — {self.producto.nombre}',
            'tipo': Movimiento.Tipo.SALIDA,
            'cantidad': '100',  # el stock es 5
            'documento': '',
            'motivo': '',
        })

        self.assertEqual(Movimiento.objects.count(), 0)
        self.assertContains(respuesta, 'Stock insuficiente')
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock_actual, 5)

    def test_salida_valida_si_actualiza_el_stock(self):
        self.client.post(reverse('inventario:movimiento_nuevo'), {
            'producto': f'{self.producto.codigo} — {self.producto.nombre}',
            'tipo': Movimiento.Tipo.SALIDA,
            'cantidad': '3',
            'documento': '',
            'motivo': '',
        })

        self.assertEqual(Movimiento.objects.count(), 1)
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock_actual, 2)


class ConteoFisicoTests(TestCase):
    def setUp(self):
        self.usuario = _crear_operador()
        self.producto = Producto.objects.create(
            codigo='PIN-902', nombre='Temple Azul',
            precio_venta='20.00', costo_compra='10.00', stock_actual=50, activo=True,
        )
        self.client.login(username='operador1', password='pass12345')

    def test_conteo_calcula_bien_la_diferencia(self):
        respuesta = self.client.post(reverse('inventario:conteo_nuevo'), {
            'fecha_corte': '2026-09-10',
            'responsable': 'Juan',
            f'stock_{self.producto.pk}': '45',
        })

        self.assertEqual(ConteoFisico.objects.count(), 1)
        conteo = ConteoFisico.objects.get()
        self.assertEqual(conteo.fecha_corte, date(2026, 9, 10))
        detalle = ConteoDetalle.objects.get(conteo=conteo, producto=self.producto)
        self.assertEqual(detalle.stock_sistema, 50)
        self.assertEqual(detalle.stock_fisico, 45)
        self.assertEqual(detalle.diferencia, -5)
        self.assertRedirects(
            respuesta, reverse('inventario:conteo_nuevo') + '?fecha=2026-09-10',
        )

    def test_guardado_parcial_no_exige_todos_los_productos(self):
        otro_producto = Producto.objects.create(
            codigo='PIN-903', nombre='Base Blanca',
            precio_venta='15.00', costo_compra='8.00', stock_actual=10, activo=True,
        )

        self.client.post(reverse('inventario:conteo_nuevo'), {
            'fecha_corte': '2026-09-11',
            'responsable': 'Juan',
            f'stock_{self.producto.pk}': '50',
            f'stock_{otro_producto.pk}': '',  # se deja en blanco a propósito
        })

        conteo = ConteoFisico.objects.get(fecha_corte=date(2026, 9, 11))
        self.assertEqual(ConteoDetalle.objects.filter(conteo=conteo).count(), 1)
        self.assertTrue(ConteoDetalle.objects.filter(conteo=conteo, producto=self.producto).exists())

    def test_continuar_un_conteo_guardado_a_medias(self):
        conteo = ConteoFisico.objects.create(
            fecha_corte=date(2026, 9, 12), responsable='Juan', origen='real',
        )
        ConteoDetalle.objects.create(
            conteo=conteo, producto=self.producto, stock_sistema=50, stock_fisico=50,
        )
        otro_producto = Producto.objects.create(
            codigo='PIN-904', nombre='Sellador Transparente',
            precio_venta='18.00', costo_compra='9.00', stock_actual=20, activo=True,
        )

        self.client.post(reverse('inventario:conteo_nuevo'), {
            'fecha_corte': '2026-09-12',
            'responsable': 'Juan',
            f'stock_{otro_producto.pk}': '18',
        })

        self.assertEqual(ConteoDetalle.objects.filter(conteo=conteo).count(), 2)
        nuevo_detalle = ConteoDetalle.objects.get(conteo=conteo, producto=otro_producto)
        self.assertEqual(nuevo_detalle.diferencia, -2)
