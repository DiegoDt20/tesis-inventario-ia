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


class DashboardOrigenTests(TestCase):
    """El dashboard (origen "real") debe coincidir con el asistente: las
    anomalías se filtran por el origen de su conteo, no del producto, y las
    recomendaciones cubren el catálogo completo."""

    def setUp(self):
        from django.utils import timezone
        from ..models import Anomalia, Recomendacion
        self.Anomalia, self.Recomendacion, self.ahora = Anomalia, Recomendacion, timezone.now()
        # Producto de catálogo cargado como "prueba", contado en un conteo real.
        self.producto = Producto.objects.create(
            codigo='PIN-D1', nombre='Latex', precio_venta=10, costo_compra=5, origen='prueba',
        )

    def _anomalia(self, origen_conteo):
        conteo = ConteoFisico.objects.create(fecha_corte=date(2026, 8, 24), responsable='Ana', origen=origen_conteo)
        detalle = ConteoDetalle.objects.create(conteo=conteo, producto=self.producto, stock_sistema=10, stock_fisico=2)
        return self.Anomalia.objects.create(
            producto=self.producto, conteo_detalle=detalle, fecha_deteccion=self.ahora,
            tipo='diferencia_inventario', severidad='media', score=0.5,
            valor_observado=-8, valor_esperado=0, descripcion='x',
        )

    def test_anomalias_por_origen_del_conteo(self):
        from ..views.anomalias import _anomalias_no_revisadas
        from ..views.dashboard import _anomalias_por_severidad
        real = self._anomalia('real')
        self._anomalia('prueba')

        self.assertEqual([a.pk for a in _anomalias_no_revisadas('real')], [real.pk])
        self.assertEqual(_anomalias_por_severidad('real')['total'], 1)

    def test_recomendaciones_de_productos_de_prueba_si_aparecen(self):
        from ..views.dashboard import _recomendaciones_urgentes, _salud_catalogo
        self.Recomendacion.objects.create(
            producto=self.producto, fecha_generacion=self.ahora, estado='critico',
            stock_actual_snapshot=0, demanda_predicha_periodo=30, desviacion_demanda=1, lead_time_usado=7,
            stock_seguridad=4, punto_reorden=11, cantidad_sugerida=34, nivel_servicio_objetivo=0.95,
            explicacion='x',
        )
        self.assertEqual(len(_recomendaciones_urgentes()), 1)
        self.assertEqual(_salud_catalogo()['total'], 1)


class PeriodoPorDefectoTests(TestCase):
    """Sin fechas en la URL, el dashboard y reportes muestran el mes
    calendario más reciente con datos (el postest), no la suma de todos los
    periodos cargados, y lo comparan con el mes anterior completo."""

    def setUp(self):
        from django.utils import timezone
        self.usuario = User.objects.create_superuser('admin_periodo', password='pass12345')
        producto = Producto.objects.create(codigo='PIN-P1', nombre='Latex', precio_venta=10, costo_compra=5, origen='real')
        # Agosto: 1 de 2 a tiempo (uno el 01/08, el día que 30 días hacia atrás
        # desde el 01/09 dejarían fuera). Setiembre: 2 de 2.
        for dia, a_tiempo in ((date(2026, 8, 1), False), (date(2026, 8, 20), True),
                              (date(2026, 9, 5), True), (date(2026, 9, 25), True)):
            pedido = Pedido.objects.create(
                fecha_solicitud=timezone.make_aware(timezone.datetime(dia.year, dia.month, dia.day)),
                cliente='C', origen='real',
            )
            PedidoDetalle.objects.create(
                pedido=pedido, producto=producto, cantidad_solicitada=1, cantidad_atendida=1,
                atendido_a_tiempo=a_tiempo,
            )

    def test_muestra_el_ultimo_mes_y_compara_con_el_mes_anterior_completo(self):
        self.client.force_login(self.usuario)
        for nombre in ('inventario:dashboard', 'inventario:reportes'):
            respuesta = self.client.get(reverse(nombre))
            self.assertEqual(respuesta.context['fecha_inicio'], date(2026, 9, 1))
            self.assertEqual(respuesta.context['fecha_fin'], date(2026, 9, 30))
            self.assertEqual(respuesta.context['ns']['valor'], 100.0)
        # NS de agosto completo = 50 %: +100 % contra el periodo anterior.
        self.assertEqual(respuesta.context['ns_anterior']['valor'], 50.0)

    def test_con_fechas_en_la_url_se_respetan(self):
        self.client.force_login(self.usuario)
        respuesta = self.client.get(reverse('inventario:dashboard'), {'fecha_inicio': '2026-08-01', 'fecha_fin': '2026-09-30'})
        self.assertEqual(respuesta.context['ns']['valor'], 75.0)

    def test_periodo_anterior(self):
        from ..views._comunes import periodo_anterior
        self.assertEqual(periodo_anterior(date(2026, 9, 1), date(2026, 9, 30)), (date(2026, 8, 1), date(2026, 8, 31)))
        self.assertEqual(periodo_anterior(date(2026, 3, 1), date(2026, 3, 31)), (date(2026, 2, 1), date(2026, 2, 28)))
        self.assertEqual(periodo_anterior(date(2026, 9, 10), date(2026, 9, 19)), (date(2026, 8, 31), date(2026, 9, 9)))


class GraficoPrediccionSerieTests(TestCase):
    """El gráfico de predicción de demanda recibe, para cada categoría, un
    punto por día sin huecos: los días sin pedidos van con 0 (nunca null ni
    ausentes) y el pronóstico empieza el día siguiente al último real."""

    def setUp(self):
        import json
        from datetime import timedelta
        from django.utils import timezone
        from ..models import ModeloEntrenado, Prediccion
        self.json, self.timedelta = json, timedelta
        self.usuario = User.objects.create_superuser('admin_grafico', password='pass12345')
        modelo = ModeloEntrenado.objects.create(
            fase=ModeloEntrenado.Fase.BASE, algoritmo='XGBRegressor', hiperparametros={},
            mae=1.0, rmse=1.0, smape=0.1, r2=0.9, ruta_archivo='artefactos/modelos_ml/test.json',
        )
        lote = timezone.now()
        # Cada categoría vende en días distintos y salteados de agosto: el
        # resto de la ventana (incluidos los días previos al primer pedido)
        # tiene que venir igual, en 0.
        ventas = {'esmalte': (3, 4, 20), 'latex': (10, 31), 'accesorio': (15,)}
        for categoria, dias in ventas.items():
            producto = Producto.objects.create(
                codigo=f'PIN-{categoria}', nombre=categoria, categoria=categoria,
                precio_venta=10, costo_compra=5, origen='real',
            )
            for dia in dias:
                pedido = Pedido.objects.create(
                    fecha_solicitud=timezone.make_aware(timezone.datetime(2026, 8, dia)), cliente='C', origen='real',
                )
                PedidoDetalle.objects.create(pedido=pedido, producto=producto, cantidad_solicitada=7, cantidad_atendida=7)
            for i in range(30):
                Prediccion.objects.create(
                    producto=producto, fecha_generacion=lote, fecha_objetivo=date(2026, 9, 1) + timedelta(days=i),
                    demanda_predicha=2.5, modelo=modelo,
                )
        self.ventas = ventas

    def test_cada_categoria_trae_un_punto_por_dia_sin_huecos(self):
        self.client.force_login(self.usuario)
        for categoria, dias in self.ventas.items():
            respuesta = self.client.get(
                reverse('inventario:dashboard_grafico_prediccion'), {'serie_grafico': f'categoria:{categoria}'},
                headers={'HX-Request': 'true'},
            )
            self.assertEqual(respuesta.context['categoria_grafico'], categoria)
            puntos = self.json.loads(respuesta.context['grafico_prediccion']['puntos_json'])
            fechas = [date.fromisoformat(p['fecha']) for p in puntos]

            # 60 días de histórico (el triple del horizonte, tope 60) + 30 de pronóstico.
            self.assertEqual(len(puntos), 90, categoria)
            self.assertEqual(fechas[0], date(2026, 7, 3))
            self.assertEqual(fechas[-1], date(2026, 9, 30))
            self.assertEqual(
                fechas, [fechas[0] + self.timedelta(days=i) for i in range(len(fechas))],
                f'{categoria}: la serie tiene días faltantes o repetidos',
            )
            for punto, fecha in zip(puntos, fechas):
                if fecha <= date(2026, 8, 31):
                    esperado = 7.0 if (fecha.month == 8 and fecha.day in dias) else 0.0
                    self.assertEqual(punto['real'], esperado, f'{categoria} {fecha}')
                    self.assertIsNone(punto['pronostico'])
                else:
                    self.assertIsNone(punto['real'])
                    self.assertEqual(punto['pronostico'], 2.5)

    def test_el_lienzo_no_hereda_tamano_ni_escala_al_cambiar_de_categoria(self):
        """Contrato del cliente: htmx no debe "asentar" style/width/height
        (le quitaba al <canvas> el tamaño fijado por Chart.js y el gráfico
        quedaba recortado, sin eje X y con el eje Y a medias) y el eje Y
        arranca en 0."""
        self.client.force_login(self.usuario)
        pagina = self.client.get(reverse('inventario:dashboard')).content.decode()
        self.assertIn('''<meta name="htmx-config" content='{"attributesToSettle": ["class"]}'>''', pagina)
        self.assertIn('htmx:beforeCleanupElement', pagina)
        self.assertRegex(pagina, r'y: \{[^}]*min: 0,')
