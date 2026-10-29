"""Carga de una ficha corregida del pretest (tarea 11).

- cargar_datos --reemplazar-mes sustituye un mes ya cargado sin duplicar
  líneas, sin borrar productos ni tocar otros meses, y sin que el stock
  real quede pisado por movimientos de prueba;
- reconstruir_catalogo deja el catálogo con lo que dice la ficha;
- comparar_fichas arma la tabla de correcciones comparando los archivos.

Las pruebas con los archivos reales de datos/ (no versionado) se saltan si
los archivos no están.
"""
import csv
from datetime import date, datetime
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest import skipUnless

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from ..models import (
    Categoria,
    ConteoFisico,
    CostoAlmacenamiento,
    Movimiento,
    Origen,
    PedidoDetalle,
    Producto,
)
from ..servicios.catalogo_ficha import separar_descripcion
from ..servicios.correcciones_ficha import comparar_fichas
from .test_congelamiento_pretest import (
    AGOSTO,
    PRODUCTOS,
    SETIEMBRE,
    _ConFichas,
    _ficha,
    _indicadores,
    _postest_setiembre,
    _pretest_agosto,
)

DATOS = Path(settings.BASE_DIR) / 'datos'
FICHA_ANTERIOR = DATOS / 'fichas_pretest_agosto_2026.xlsx'
FICHA_CORREGIDA = DATOS / 'PRETEST_TESIS_FINAL.xlsx'
HAY_FICHAS_REALES = FICHA_ANTERIOR.exists() and FICHA_CORREGIDA.exists()


def _pretest_corregido(ruta):
    """El pretest de _pretest_agosto con las correcciones típicas de la
    ficha final: el pedido 2 completado (10 -> 12 atendidas, sale del PD),
    el pedido 4 con otro código, conteo al 31/08 y CA 01/08–31/08."""
    d = lambda dia: date(2026, 8, dia)  # noqa: E731
    return _ficha(
        ruta,
        conteo=(d(31), [
            ('PIN-001', PRODUCTOS[0][1], 12, 10),
            ('PIN-002', PRODUCTOS[1][1], 6, 6),
            ('PIN-003', PRODUCTOS[2][1], 14, 16),
            ('PIN-004', PRODUCTOS[3][1], 15, 15),
        ]),
        pedidos=[
            (1, d(1), 'C-001', 'PIN-001', PRODUCTOS[0][1], 8, 8, d(1), d(1), 1),
            (2, d(5), 'C-002', 'PIN-001', PRODUCTOS[0][1], 12, 12, d(5), d(5), 1),
            (3, d(12), 'C-003', 'PIN-002', PRODUCTOS[1][1], 5, 0, d(13), None, 0),
            (4, d(20), 'C-004', 'PIN-004', PRODUCTOS[3][1], 2, 2, d(21), d(22), 0),
            (5, d(31), 'C-005', 'PIN-004', PRODUCTOS[3][1], 3, 3, d(31), d(31), 1),
        ],
        costos=('01/08/2026–31/08/2026', [
            ('Alquiler imputado al almacén', 550),
            ('Energía eléctrica imputada', 50),
            ('Personal de almacén', 300),
            ('Mermas del periodo', 150),
        ]),
        # PD = 5 × (120 − 90) = 150
        perdidas=[(3, 'PIN-002', 5, 120, 90)],
    )


def _lineas(desde, hasta):
    return PedidoDetalle.objects.filter(
        pedido__origen=Origen.REAL,
        pedido__fecha_solicitud__date__gte=desde, pedido__fecha_solicitud__date__lte=hasta,
    )


class ReemplazarMesTests(_ConFichas):
    def setUp(self):
        super().setUp()
        self._cargar_pretest()

    def _reemplazar(self, ruta=None, **opciones):
        ruta = ruta or _pretest_corregido(self.ruta('corregido.xlsx'))
        return self._cargar(ruta, reemplazar_mes='2026-08', **opciones)

    def test_sustituye_las_lineas_sin_duplicarlas(self):
        self._reemplazar()
        self.assertEqual(_lineas(*AGOSTO).count(), 5)
        self.assertEqual(ConteoFisico.objects.filter(origen=Origen.REAL).count(), 1)
        self.assertEqual(CostoAlmacenamiento.objects.filter(origen=Origen.REAL).count(), 4)

    def test_indicadores_salen_de_la_ficha_corregida(self):
        self._reemplazar()
        ind = _indicadores(*AGOSTO)
        self.assertEqual((ind['ei']['correctos'], ind['ei']['total']), (2, 4))
        self.assertEqual((ind['ns']['a_tiempo'], ind['ns']['total']), (3, 5))
        self.assertEqual(ind['coi']['valor'], Decimal('1200.00'))
        self.assertEqual(ind['coi']['lineas_desabastecimiento'], 1)

    def test_fecha_de_corte_y_periodo_del_ca(self):
        salida = self._reemplazar()
        conteo = ConteoFisico.objects.get(origen=Origen.REAL)
        self.assertEqual(conteo.fecha_corte, date(2026, 8, 31))
        self.assertEqual(
            set(CostoAlmacenamiento.objects.filter(origen=Origen.REAL).values_list('periodo_mes', flat=True)),
            {date(2026, 8, 1)},
        )
        # La ventana declarada (01/08–31/08) es la de los pedidos: sin aviso.
        self.assertNotIn('miden ventanas distintas', salida)

    def test_precios_congelados_por_linea(self):
        self._reemplazar()
        antes = _indicadores(*AGOSTO)['coi']['valor']
        Producto.objects.filter(codigo='PIN-002').update(precio_venta=Decimal('999.00'), costo_compra=Decimal('1.00'))
        self.assertEqual(_indicadores(*AGOSTO)['coi']['valor'], antes)
        linea = _lineas(*AGOSTO).get(producto__codigo='PIN-002')
        self.assertEqual((linea.precio_venta_unitario, linea.costo_compra_unitario), (Decimal('120.00'), Decimal('90.00')))

    def test_no_borra_productos_ni_otros_meses(self):
        self._cargar(_postest_setiembre(self.ruta('postest.xlsx')))
        setiembre = _indicadores(*SETIEMBRE)
        productos = set(Producto.objects.values_list('pk', flat=True))
        self._reemplazar()
        self.assertEqual(set(Producto.objects.values_list('pk', flat=True)), productos)
        self.assertEqual(_indicadores(*SETIEMBRE), setiembre)
        self.assertEqual(_lineas(*SETIEMBRE).count(), 3)

    def test_rechaza_un_archivo_de_otro_mes_sin_escribir(self):
        lineas = set(_lineas(*AGOSTO).values_list('pk', flat=True))
        with self.assertRaisesMessage(CommandError, 'debe traer solo 08/2026'):
            self._reemplazar(_postest_setiembre(self.ruta('postest.xlsx')))
        self.assertEqual(set(_lineas(*AGOSTO).values_list('pk', flat=True)), lineas)

    def test_dry_run_no_escribe(self):
        lineas = set(_lineas(*AGOSTO).values_list('pk', flat=True))
        self._reemplazar(dry_run=True)
        self.assertEqual(set(_lineas(*AGOSTO).values_list('pk', flat=True)), lineas)
        self.assertEqual(ConteoFisico.objects.get(origen=Origen.REAL).fecha_corte, date(2026, 8, 24))

    def test_no_se_combina_con_limpiar(self):
        with self.assertRaisesMessage(CommandError, 'no se pueden usar juntos'):
            self._reemplazar(limpiar=True)

    def test_stock_real_no_lo_pisa_un_ajuste_de_prueba_posterior(self):
        producto = Producto.objects.get(codigo='PIN-001')
        Movimiento.objects.create(
            producto=producto, tipo=Movimiento.Tipo.AJUSTE, cantidad=99,
            fecha=timezone.make_aware(datetime(2026, 9, 10)), motivo='conteo de prueba',
            usuario=User.objects.create(username='prueba'), origen=Origen.PRUEBA,
        )
        self._reemplazar()
        producto.refresh_from_db()
        # 12 = kardex del conteo real del 31/08, no el ajuste de prueba (99).
        self.assertEqual(producto.stock_actual, 12)


class ResponsableConteoTests(_ConFichas):
    def test_por_defecto_importacion_automatica(self):
        self._cargar_pretest()
        self.assertEqual(ConteoFisico.objects.get(origen=Origen.REAL).responsable, 'Importación automática')

    def test_se_puede_indicar(self):
        self._cargar(_pretest_agosto(self.ruta('pretest.xlsx')), responsable='Investigadores')
        self.assertEqual(ConteoFisico.objects.get(origen=Origen.REAL).responsable, 'Investigadores')


class RecalcularStockPorOrigenTests(_ConFichas):
    def test_con_origen_ignora_los_movimientos_de_otro_origen(self):
        self._cargar_pretest()
        producto = Producto.objects.get(codigo='PIN-001')
        Movimiento.objects.create(
            producto=producto, tipo=Movimiento.Tipo.AJUSTE, cantidad=99,
            fecha=timezone.make_aware(datetime(2026, 9, 10)), motivo='conteo de prueba',
            usuario=User.objects.create(username='prueba'), origen=Origen.PRUEBA,
        )
        call_command('recalcular_stock', origen='real', stdout=StringIO())
        producto.refresh_from_db()
        self.assertEqual(producto.stock_actual, 12)
        call_command('recalcular_stock', stdout=StringIO())
        producto.refresh_from_db()
        self.assertEqual(producto.stock_actual, 99)


def _ficha_catalogo(ruta):
    """Ficha con descripciones como las reales (sin separadores)."""
    d = date(2026, 8, 31)
    return _ficha(
        ruta,
        conteo=(d, [
            ('PIN-219', 'Anypsa Gloss Azul honda balde 1 galón', 14, 18),
            ('PIN-260', 'Lija Grano 090 pliego', 26, 26),
            ('PIN-300', 'Innova Coral balde 1 galón', 5, 5),
            ('PIN-301', 'Innova Coral balde 1 galón', 7, 7),
        ]),
        pedidos=[(1, d, 'C-1', 'PIN-260', 'Lija Grano 090 pliego', 18, 9, d, d, 0)],
        costos=('01/08/2026–31/08/2026', [('Alquiler imputado al almacén', 550)]),
        perdidas=[(1, 'PIN-260', 9, 1.2, 0.8)],
    )


class ReconstruirCatalogoTests(_ConFichas):
    def setUp(self):
        super().setUp()
        crear = lambda codigo, nombre, color, presentacion, categoria, precio: Producto.objects.create(  # noqa: E731
            codigo=codigo, nombre=nombre, color=color, presentacion=presentacion, categoria=categoria,
            precio_venta=Decimal(precio), costo_compra=Decimal(precio) - 5,
        )
        crear('PIN-219', 'Anypsa Gloss', 'Azul honda', '1/16 galón', Categoria.ESMALTE, '48')
        crear('PIN-260', 'Anypsa Gloss', 'Azul honda', 'balde 4 galones', Categoria.ESMALTE, '30')
        crear('PIN-261', 'Lija Grano 100 pliego', '', '', Categoria.ACCESORIO, '30')
        crear('PIN-300', 'Innova', 'Coral', 'galón', Categoria.OTRO, '33')
        crear('PIN-301', 'Innova', 'Coral', '1/4 galón', Categoria.LATEX, '20')
        self.archivo = _ficha_catalogo(self.ruta('ficha.xlsx'))

    def _reconstruir(self, **opciones):
        salida = StringIO()
        call_command('reconstruir_catalogo', archivo=str(self.archivo), stdout=salida, **opciones)
        return salida.getvalue()

    def test_nombre_y_presentacion_de_la_ficha(self):
        self._reconstruir(categoria=['PIN-260=accesorio'])
        p = Producto.objects.get(codigo='PIN-260')
        self.assertEqual((p.nombre, p.color, p.presentacion, p.categoria), ('Lija', 'Grano 090', 'pliego', 'accesorio'))
        p = Producto.objects.get(codigo='PIN-219')
        self.assertEqual(p.presentacion, 'balde 1 galón')

    def test_precio_solo_de_los_productos_del_pd(self):
        self._reconstruir()
        self.assertEqual(Producto.objects.get(codigo='PIN-260').precio_venta, Decimal('1.20'))
        self.assertEqual(Producto.objects.get(codigo='PIN-219').precio_venta, Decimal('48.00'))

    def test_categoria_conservada_y_deducida(self):
        salida = self._reconstruir()
        self.assertEqual(Producto.objects.get(codigo='PIN-219').categoria, Categoria.ESMALTE)
        # Estaba en "otro": se deduce del nombre (Innova -> látex).
        self.assertEqual(Producto.objects.get(codigo='PIN-300').categoria, Categoria.LATEX)
        # Sin forzarla, PIN-260 conserva esmalte y se reporta el conflicto.
        self.assertEqual(Producto.objects.get(codigo='PIN-260').categoria, Categoria.ESMALTE)
        self.assertIn('PIN-260  Lija Grano 090 pliego: esmalte (el nombre sugiere accesorio)', salida)

    def test_los_que_no_estan_en_la_ficha_quedan_inactivos(self):
        self._reconstruir()
        p = Producto.objects.get(codigo='PIN-261')
        self.assertFalse(p.activo)
        self.assertEqual(Producto.objects.filter(activo=True).count(), 4)

    def test_reporta_descripciones_repetidas(self):
        salida = self._reconstruir()
        self.assertIn('Innova Coral balde 1 galón: PIN-300, PIN-301', salida)

    def test_tabla_de_cambios_y_dry_run(self):
        base = self.ruta('cambios')
        self._reconstruir(tabla=str(base), dry_run=True)
        self.assertEqual(Producto.objects.get(codigo='PIN-260').nombre, 'Anypsa Gloss')
        with open(base.with_suffix('.csv'), encoding='utf-8') as archivo:
            filas = list(csv.DictReader(archivo))
        self.assertIn(
            {'Código': 'PIN-260', 'Campo': 'Nombre', 'Valor anterior': 'Anypsa Gloss', 'Valor nuevo': 'Lija'}, filas,
        )
        self.assertIn(
            {'Código': 'PIN-261', 'Campo': 'Activo', 'Valor anterior': 'Sí', 'Valor nuevo': 'No'}, filas,
        )

    def test_separar_descripcion(self):
        self.assertEqual(
            separar_descripcion('Anypsa Gloss Azul honda balde 1 galón'),
            ('Anypsa Gloss', 'Azul honda', 'balde 1 galón'),
        )
        self.assertEqual(separar_descripcion('Brocha Tumi 2 pulgadas unidad'), ('Brocha Tumi', '2 pulgadas', 'unidad'))
        self.assertIsNone(separar_descripcion('Marca Desconocida Rojo galón'))


class CompararFichasTests(_ConFichas):
    def test_clasifica_registro_periodo_y_otras(self):
        registro, periodo, otras = comparar_fichas(
            _pretest_agosto(self.ruta('a.xlsx')), _pretest_corregido(self.ruta('b.xlsx')),
        )
        self.assertEqual(
            [(c.registro, c.campo, c.anterior, c.corregido) for c in registro],
            [(2, 'Cant. atendida', 10, 12), (4, 'Código del producto', 'PIN-003', 'PIN-004')],
        )
        self.assertEqual([(c.hoja, c.anterior, c.corregido) for c in periodo], [
            ('1_EI', '24/08/2026', '31/08/2026'),
            ('3_COI_CA', '25/07/2026–24/08/2026', '01/08/2026–31/08/2026'),
        ])
        # El pedido 2 sale del bloque PD y su "¿A tiempo?" pasa a 1.
        self.assertIn(('3_COI_PD', 'pedido 2', '(fila completa)'), [(c.hoja, c.registro, c.campo) for c in otras])
        self.assertIn(('2_NS', 2, '¿A tiempo? (1/0)'), [(c.hoja, c.registro, c.campo) for c in otras])


@skipUnless(HAY_FICHAS_REALES, 'faltan las fichas reales en datos/')
class FichaRealCorregidaTests(_ConFichas):
    """Con los archivos reales: la tabla y los indicadores esperados."""

    def test_tabla_de_correcciones_8_registros_y_2_periodos(self):
        registro, periodo, _otras = comparar_fichas(FICHA_ANTERIOR, FICHA_CORREGIDA)
        self.assertEqual(sorted({c.registro for c in registro}), [26, 47, 162, 167, 190, 229, 240, 241])
        # 9 campos: el 167 corrige cantidad atendida y fecha de atención.
        self.assertEqual(len(registro), 9)
        self.assertEqual(len(periodo), 2)

    def test_reemplazo_da_los_indicadores_esperados(self):
        self._cargar(FICHA_ANTERIOR)
        self._cargar(FICHA_CORREGIDA, reemplazar_mes='2026-08')
        self.assertEqual(_lineas(*AGOSTO).count(), 260)
        ind = _indicadores(*AGOSTO)
        self.assertEqual((ind['ei']['correctos'], ind['ei']['total']), (146, 260))
        self.assertEqual((ind['ns']['a_tiempo'], ind['ns']['total']), (154, 260))
        self.assertEqual(ind['coi']['valor'], Decimal('5341.40'))
        self.assertEqual(ind['coi']['lineas_desabastecimiento'], 90)
        self.assertEqual(ConteoFisico.objects.get(origen=Origen.REAL).fecha_corte, date(2026, 8, 31))
