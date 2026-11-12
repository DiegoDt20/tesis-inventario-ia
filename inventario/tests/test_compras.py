"""Tests de las pantallas de catálogo (productos, proveedores) y de
compras: registro, orden desde recomendaciones aceptadas, recepción y
cierre."""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ..decisiones.motor import _pedidos_en_transito
from ..forms import etiqueta_producto
from ..models import Compra, CompraDetalle, Movimiento, Origen, Producto, Proveedor, Recomendacion
from .test_vistas import _crear_operador


def _producto(codigo='PIN-001', **extra):
    datos = dict(
        codigo=codigo, nombre=f'Látex {codigo}', presentacion='galón',
        precio_venta='50.00', costo_compra='30.00', stock_actual=10, origen=Origen.REAL,
    )
    datos.update(extra)
    return Producto.objects.create(**datos)


def _recomendacion(producto, fecha, cantidad=12.0, aceptada=True, estado=Recomendacion.Estado.CRITICO):
    return Recomendacion.objects.create(
        producto=producto, fecha_generacion=fecha, estado=estado, stock_actual_snapshot=producto.stock_actual,
        demanda_predicha_periodo=20.0, desviacion_demanda=1.0, lead_time_usado=7.0, stock_seguridad=2.0,
        punto_reorden=9.0, cantidad_sugerida=cantidad, nivel_servicio_objetivo=0.95, explicacion='x',
        aceptada=aceptada,
    )


class _ConAdmin(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser('admin', 'a@a.pe', 'x')
        self.client.force_login(self.admin)


class PermisosTests(TestCase):
    def test_operador_ve_productos_pero_no_edita_ni_entra_a_compras(self):
        _crear_operador()
        self.client.login(username='operador1', password='pass12345')
        producto = _producto()
        self.assertEqual(self.client.get(reverse('inventario:producto_lista')).status_code, 200)
        for nombre, args in (
            ('producto_nuevo', []), ('producto_editar', [producto.pk]), ('proveedor_lista', []),
            ('proveedor_nuevo', []), ('compra_lista', []), ('compra_nueva', []),
        ):
            self.assertEqual(self.client.get(reverse(f'inventario:{nombre}', args=args)).status_code, 403, nombre)

    def test_sin_sesion_redirige_al_login(self):
        self.assertEqual(self.client.get(reverse('inventario:compra_lista')).status_code, 302)


class ProductosTests(_ConAdmin):
    def _datos(self, **extra):
        datos = {
            'codigo': 'pin-777', 'nombre': 'Esmalte rojo', 'presentacion': 'galón', 'color': 'rojo', 'marca': '',
            'categoria': 'esmalte', 'precio_venta': '60.00', 'costo_compra': '40.00', 'stock_minimo': '3',
            'lead_time_dias': '7', 'activo': 'on',
        }
        datos.update(extra)
        return datos

    def test_crear_producto_queda_real_y_con_stock_cero(self):
        respuesta = self.client.post(reverse('inventario:producto_nuevo'), self._datos(stock_actual='99'))
        self.assertRedirects(respuesta, reverse('inventario:producto_lista'))
        producto = Producto.objects.get(codigo='PIN-777')
        self.assertEqual(producto.origen, Origen.REAL)
        self.assertEqual(producto.stock_actual, 0)   # el stock no es un campo del formulario

    def test_codigo_repetido_y_valores_invalidos(self):
        _producto('PIN-777')
        respuesta = self.client.post(reverse('inventario:producto_nuevo'), self._datos())
        self.assertEqual(respuesta.status_code, 200)
        self.assertIn('codigo', respuesta.context['form'].errors)

        respuesta = self.client.post(
            reverse('inventario:producto_nuevo'),
            self._datos(codigo='PIN-778', precio_venta='10.00', lead_time_dias='0', stock_minimo='-1'),
        )
        errores = respuesta.context['form'].errors
        self.assertEqual(set(errores), {'precio_venta', 'lead_time_dias', 'stock_minimo'})

    def test_editar_no_cambia_codigo_ni_stock(self):
        producto = _producto('PIN-001', stock_actual=10)
        respuesta = self.client.post(
            reverse('inventario:producto_editar', args=[producto.pk]),
            self._datos(codigo='OTRO-1', nombre='Nombre nuevo', stock_actual='500'),
        )
        self.assertRedirects(respuesta, reverse('inventario:producto_lista'))
        producto.refresh_from_db()
        self.assertEqual((producto.codigo, producto.nombre, producto.stock_actual), ('PIN-001', 'Nombre nuevo', 10))
        self.assertEqual(producto.precio_venta, Decimal('60.00'))

    def test_lista_filtra_por_texto_y_oculta_inactivos(self):
        _producto('PIN-001')
        _producto('PIN-002', nombre='Thinner', activo=False)
        url = reverse('inventario:producto_lista')
        self.assertEqual([p.codigo for p in self.client.get(url).context['pagina']], ['PIN-001'])
        self.assertEqual(
            [p.codigo for p in self.client.get(url, {'q': 'thin', 'estado': 'todos'}).context['pagina']], ['PIN-002'],
        )


class ProveedoresTests(_ConAdmin):
    def test_crear_y_editar(self):
        respuesta = self.client.post(reverse('inventario:proveedor_nuevo'), {
            'nombre': 'Pinturas del Sur', 'contacto': 'Rosa', 'telefono': '999', 'lead_time_promedio': '5',
        })
        self.assertRedirects(respuesta, reverse('inventario:proveedor_lista'))
        proveedor = Proveedor.objects.get()
        self.client.post(reverse('inventario:proveedor_editar', args=[proveedor.pk]), {
            'nombre': 'Pinturas del Sur SAC', 'contacto': '', 'telefono': '', 'lead_time_promedio': '6',
        })
        proveedor.refresh_from_db()
        self.assertEqual((proveedor.nombre, proveedor.lead_time_promedio), ('Pinturas del Sur SAC', 6))

    def test_desde_la_compra_vuelve_a_la_compra_y_no_a_una_url_externa(self):
        datos = {'nombre': 'A', 'contacto': '', 'telefono': '', 'lead_time_promedio': '0'}
        respuesta = self.client.post(reverse('inventario:proveedor_nuevo'), {**datos, 'siguiente': 'compra'})
        self.assertRedirects(respuesta, reverse('inventario:compra_nueva'))
        respuesta = self.client.post(
            reverse('inventario:proveedor_nuevo'), {**datos, 'nombre': 'B', 'siguiente': 'https://malo.example'},
        )
        self.assertRedirects(respuesta, reverse('inventario:proveedor_lista'))


class _ConCompra(_ConAdmin):
    def setUp(self):
        super().setUp()
        self.proveedor = Proveedor.objects.create(nombre='Pinturas del Sur')
        self.p1 = _producto('PIN-001')
        self.p2 = _producto('PIN-002')
        self.hoy = date.today()

    def _post_compra(self, lineas, **extra):
        datos = {
            'proveedor': self.proveedor.pk, 'fecha_pedido': (self.hoy - timedelta(days=3)).isoformat(),
            'form-TOTAL_FORMS': str(len(lineas)), 'form-INITIAL_FORMS': '0',
            'form-MIN_NUM_FORMS': '0', 'form-MAX_NUM_FORMS': '1000', 'guardar': '1',
        }
        for i, linea in enumerate(lineas):
            for campo, valor in linea.items():
                datos[f'form-{i}-{campo}'] = valor
        datos.update(extra)
        return self.client.post(reverse('inventario:compra_nueva'), datos)

    def _linea(self, producto, cantidad=10, costo='30.00', **extra):
        return {'producto': etiqueta_producto(producto), 'cantidad_pedida': cantidad, 'costo_unitario': costo, **extra}

    def _compra(self, cantidades=(10, 4)):
        compra = Compra.objects.create(proveedor=self.proveedor, fecha_pedido=self.hoy - timedelta(days=3))
        lineas = [
            CompraDetalle.objects.create(compra=compra, producto=p, cantidad_pedida=c, costo_unitario='30.00')
            for p, c in zip((self.p1, self.p2), cantidades)
        ]
        return compra, lineas

    def _recibir(self, compra, cantidades, fecha=None):
        datos = {f'recibir_{pk}': str(c) for pk, c in cantidades.items()}
        datos['fecha_llegada'] = (fecha or self.hoy).isoformat()
        return self.client.post(reverse('inventario:compra_recibir', args=[compra.pk]), datos, follow=True)


class RegistrarCompraTests(_ConCompra):
    def test_compra_manual_no_toca_el_stock_y_cuenta_en_transito(self):
        respuesta = self._post_compra([self._linea(self.p1, 10), self._linea(self.p2, 4, '12.50')])
        compra = Compra.objects.get()
        self.assertRedirects(respuesta, reverse('inventario:compra_detalle', args=[compra.pk]))
        self.assertEqual(compra.estado, Compra.Estado.PENDIENTE)
        self.assertEqual(compra.detalles.count(), 2)
        self.p1.refresh_from_db()
        self.assertEqual(self.p1.stock_actual, 10)
        self.assertEqual(Movimiento.objects.count(), 0)
        self.assertEqual(_pedidos_en_transito(self.p1), 10)

    def test_sin_lineas_producto_invalido_o_repetido_no_guarda(self):
        self._post_compra([self._linea(self.p1, 10, DELETE='on')])
        respuesta = self._post_compra([{'producto': 'no existe', 'cantidad_pedida': 1, 'costo_unitario': '1'}])
        self.assertEqual(respuesta.status_code, 200)
        respuesta = self._post_compra([self._linea(self.p1, 1), self._linea(self.p1, 2)])
        self.assertContains(respuesta, 'está en más de una línea')
        self.assertEqual(Compra.objects.count(), 0)

    def test_agregar_linea_no_valida_ni_guarda(self):
        respuesta = self._post_compra([self._linea(self.p1, 10)], agregar_linea='1')
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(len(respuesta.context['formset'].forms), 2)
        self.assertEqual(Compra.objects.count(), 0)


class CompraDesdeRecomendacionesTests(_ConCompra):
    def setUp(self):
        super().setUp()
        ahora = timezone.now()
        self.aceptada = _recomendacion(self.p1, ahora, cantidad=12.4)
        _recomendacion(self.p2, ahora, aceptada=None)                       # sin decidir
        p3, p4 = _producto('PIN-003'), _producto('PIN-004')
        _recomendacion(p3, ahora, aceptada=False)                           # rechazada
        _recomendacion(p4, ahora, cantidad=0.0)                             # aceptada pero no sugiere comprar
        _recomendacion(self.p2, ahora - timedelta(days=5), cantidad=50.0)   # lote anterior
        self.url = reverse('inventario:compra_nueva') + '?desde=recomendaciones'

    def test_precarga_solo_las_aceptadas_del_ultimo_lote_que_sugieren_comprar(self):
        formset = self.client.get(self.url).context['formset']
        iniciales = [f.initial for f in formset.forms if f.initial]
        self.assertEqual(len(iniciales), 1)
        self.assertEqual(iniciales[0]['producto'], etiqueta_producto(self.p1))
        self.assertEqual(iniciales[0]['cantidad_pedida'], 12)
        self.assertEqual(iniciales[0]['costo_unitario'], Decimal('30.00'))
        self.assertEqual(iniciales[0]['recomendacion'], self.aceptada.pk)

    def test_la_linea_queda_enlazada_y_no_se_vuelve_a_ofrecer(self):
        self._post_compra([self._linea(self.p1, 12, recomendacion=self.aceptada.pk)])
        linea = CompraDetalle.objects.get()
        self.assertEqual(linea.recomendacion, self.aceptada)

        respuesta = self.client.get(self.url)
        self.assertEqual([f.initial for f in respuesta.context['formset'].forms if f.initial], [])
        self.assertContains(respuesta, 'No hay recomendaciones aceptadas pendientes de pedir.')
        self.assertContains(
            self.client.get(reverse('inventario:recomendaciones_lista')), f'En la compra #{linea.compra_id}',
        )

    def test_una_compra_cancelada_libera_la_recomendacion(self):
        self._post_compra([self._linea(self.p1, 12, recomendacion=self.aceptada.pk)])
        compra = Compra.objects.get()
        self.client.post(reverse('inventario:compra_cerrar', args=[compra.pk]))
        compra.refresh_from_db()
        self.assertEqual(compra.estado, Compra.Estado.CANCELADA)
        formset = self.client.get(self.url).context['formset']
        self.assertEqual(len([f for f in formset.forms if f.initial]), 1)

    def test_no_enlaza_una_recomendacion_de_otro_producto_ni_sin_aceptar(self):
        sin_decidir = Recomendacion.objects.get(producto=self.p2, aceptada=None)
        self._post_compra([
            self._linea(self.p2, 5, recomendacion=self.aceptada.pk),   # es de p1
        ])
        self.assertIsNone(CompraDetalle.objects.get().recomendacion)
        CompraDetalle.objects.all().delete()
        self._post_compra([self._linea(self.p2, 5, recomendacion=sin_decidir.pk)])
        self.assertIsNone(CompraDetalle.objects.get().recomendacion)


class RecibirCompraTests(_ConCompra):
    def test_recepcion_parcial_y_luego_completa(self):
        compra, (l1, l2) = self._compra((10, 4))
        ayer = self.hoy - timedelta(days=1)

        self._recibir(compra, {l1.pk: 6, l2.pk: 0}, fecha=ayer)
        compra.refresh_from_db()
        self.p1.refresh_from_db()
        self.p2.refresh_from_db()
        self.assertEqual(compra.estado, Compra.Estado.PENDIENTE)
        self.assertEqual((self.p1.stock_actual, self.p2.stock_actual), (16, 10))
        self.assertEqual(_pedidos_en_transito(self.p1), 4)
        movimiento = Movimiento.objects.get()
        self.assertEqual(
            (movimiento.tipo, movimiento.cantidad, movimiento.origen, movimiento.usuario, movimiento.documento),
            (Movimiento.Tipo.INGRESO, 6, Origen.REAL, self.admin, f'Compra #{compra.pk}'),
        )
        self.assertEqual(timezone.localtime(movimiento.fecha).date(), ayer)

        self._recibir(compra, {l1.pk: 4, l2.pk: 4})
        compra.refresh_from_db()
        self.p1.refresh_from_db()
        self.p2.refresh_from_db()
        self.assertEqual(compra.estado, Compra.Estado.RECIBIDA)
        self.assertEqual(compra.fecha_llegada, self.hoy)
        self.assertEqual((self.p1.stock_actual, self.p2.stock_actual), (20, 14))
        self.assertEqual(_pedidos_en_transito(self.p1), 0)
        self.assertEqual(Movimiento.objects.count(), 3)

    def test_cantidades_o_fechas_invalidas_no_registran_nada(self):
        compra, (l1, l2) = self._compra((10, 4))
        casos = [
            ({l1.pk: 11, l2.pk: 0}, None, 'debe estar entre 0 y 10'),
            ({l1.pk: -1, l2.pk: 0}, None, 'debe estar entre 0 y 10'),
            ({l1.pk: 0, l2.pk: 0}, None, 'Indica cuántas unidades llegaron'),
            ({l1.pk: 5, l2.pk: 0}, compra.fecha_pedido - timedelta(days=1), 'no puede ser anterior'),
            ({l1.pk: 5, l2.pk: 0}, self.hoy + timedelta(days=1), 'no puede ser futura'),
        ]
        for cantidades, fecha, mensaje in casos:
            self.assertContains(self._recibir(compra, cantidades, fecha=fecha), mensaje)
        # Una línea válida junto a una inválida tampoco se registra.
        self._recibir(compra, {l1.pk: 5, l2.pk: 99})
        self.assertEqual(Movimiento.objects.count(), 0)
        self.p1.refresh_from_db()
        self.assertEqual(self.p1.stock_actual, 10)

    def test_no_se_recibe_una_compra_ya_cerrada(self):
        compra, (l1, l2) = self._compra((10, 4))
        self._recibir(compra, {l1.pk: 10, l2.pk: 4})
        self.assertContains(self._recibir(compra, {l1.pk: 1, l2.pk: 0}), 'Solo se puede recibir mercadería de una compra pendiente')
        self.assertEqual(Movimiento.objects.count(), 2)


class CerrarCompraTests(_ConCompra):
    def test_sin_nada_recibido_se_cancela(self):
        compra, _ = self._compra()
        self.client.post(reverse('inventario:compra_cerrar', args=[compra.pk]))
        compra.refresh_from_db()
        self.assertEqual(compra.estado, Compra.Estado.CANCELADA)
        self.assertEqual(_pedidos_en_transito(self.p1), 0)
        self.assertEqual(compra.detalles.count(), 2)

    def test_con_una_parte_recibida_queda_recibida_y_libera_lo_que_faltaba(self):
        compra, (l1, l2) = self._compra((10, 4))
        self._recibir(compra, {l1.pk: 6, l2.pk: 0})
        self.client.post(reverse('inventario:compra_cerrar', args=[compra.pk]))
        compra.refresh_from_db()
        l1.refresh_from_db()
        self.assertEqual(compra.estado, Compra.Estado.RECIBIDA)
        self.assertEqual(compra.fecha_llegada, self.hoy)
        self.assertEqual((l1.cantidad_pedida, l1.cantidad_recibida), (6, 6))
        self.assertFalse(CompraDetalle.objects.filter(pk=l2.pk).exists())
        self.assertEqual(_pedidos_en_transito(self.p1), 0)
        self.assertEqual(_pedidos_en_transito(self.p2), 0)
        # Cerrar no genera movimientos ni cambia el stock ya recibido.
        self.assertEqual(Movimiento.objects.count(), 1)
        self.p1.refresh_from_db()
        self.assertEqual(self.p1.stock_actual, 16)

    def test_exige_post(self):
        compra, _ = self._compra()
        self.assertEqual(self.client.get(reverse('inventario:compra_cerrar', args=[compra.pk])).status_code, 405)


class PantallasCompraTests(_ConCompra):
    def test_lista_y_detalle(self):
        compra, (l1, _) = self._compra((10, 4))
        _recomendacion(self.p1, timezone.now())
        respuesta = self.client.get(reverse('inventario:compra_lista'))
        self.assertContains(respuesta, 'Pinturas del Sur')
        self.assertContains(respuesta, '0 de 14 u.')
        self.assertContains(respuesta, 'S/ 420,00')
        self.assertContains(respuesta, 'Comprar lo aceptado (1)')
        respuesta = self.client.get(reverse('inventario:compra_detalle', args=[compra.pk]))
        self.assertContains(respuesta, f'name="recibir_{l1.pk}"')
        self.assertContains(respuesta, 'Cancelar compra')
        self.assertEqual(self.client.get(reverse('inventario:proveedor_lista')).status_code, 200)
