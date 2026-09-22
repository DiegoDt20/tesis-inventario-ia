"""Tests de clasificación de productos por categoría y de selección de
categorías aptas para el motor de predicción por categoría
(inventario/ml/carga_interna.py)."""
import pandas as pd
from django.test import SimpleTestCase

from inventario.management.commands.clasificar_productos import clasificar_producto
from inventario.ml.carga_interna import seleccionar_categorias_aptas
from inventario.models import Categoria, Producto


def _producto_sin_guardar(nombre, color='', marca=''):
    """Producto en memoria (sin guardar en la base) para probar
    clasificar_producto, que solo lee atributos en Python."""
    return Producto(
        codigo='TMP', nombre=nombre, color=color, marca=marca,
        precio_venta='1.00', costo_compra='1.00',
    )


class ClasificarProductoTests(SimpleTestCase):
    def test_accesorio_se_evalua_antes_que_base(self):
        # "base de rodillo" es un accesorio, no una base: accesorio debe
        # evaluarse primero.
        producto = _producto_sin_guardar('Base de rodillo 4 pulgadas')
        self.assertEqual(clasificar_producto(producto), Categoria.ACCESORIO)

    def test_solvente_se_evalua_antes_que_esmalte(self):
        producto = _producto_sin_guardar('Thinner acrílico brillante')
        self.assertEqual(clasificar_producto(producto), Categoria.SOLVENTE)

    def test_busca_tambien_en_el_campo_color(self):
        # En los datos reales el tipo de producto a veces está en "color" y
        # "nombre" solo trae la marca (p. ej. nombre="Anypsa").
        producto = _producto_sin_guardar('Anypsa', color='Thinner extra acrílico')
        self.assertEqual(clasificar_producto(producto), Categoria.SOLVENTE)

        producto = _producto_sin_guardar('Velsalit', color='Sellador antisalitre')
        self.assertEqual(clasificar_producto(producto), Categoria.BASE)

    def test_ignora_tildes(self):
        producto = _producto_sin_guardar('Látex Blanco premium')
        self.assertEqual(clasificar_producto(producto), Categoria.LATEX)

    def test_marca_conocida_clasifica_como_latex(self):
        producto = _producto_sin_guardar('Innova Marfil congo balde 1 galón')
        self.assertEqual(clasificar_producto(producto), Categoria.LATEX)

    def test_sin_ninguna_palabra_clave_queda_en_otro(self):
        producto = _producto_sin_guardar('Leon', color='marron')
        self.assertEqual(clasificar_producto(producto), Categoria.OTRO)


class SeleccionarCategoriasAptasTests(SimpleTestCase):
    def test_separa_por_umbral_de_dias_con_venta(self):
        fechas_muchas = pd.date_range('2026-01-01', periods=20, freq='D')
        fechas_pocas = pd.date_range('2026-01-01', periods=5, freq='D')
        df = pd.concat([
            pd.DataFrame({'fecha': fechas_muchas, 'serie_id': 'latex', 'cantidad': 1}),
            pd.DataFrame({'fecha': fechas_pocas, 'serie_id': 'base', 'cantidad': 1}),
        ], ignore_index=True)

        aptas, no_aptas, dias = seleccionar_categorias_aptas(df, min_dias_venta=15)

        self.assertEqual(aptas, ['latex'])
        self.assertEqual(no_aptas, ['base'])
        self.assertEqual(dias['latex'], 20)
        self.assertEqual(dias['base'], 5)

    def test_dias_sin_venta_no_cuentan_como_dia_con_venta(self):
        df = pd.DataFrame({
            'fecha': pd.date_range('2026-01-01', periods=20, freq='D'),
            'serie_id': 'esmalte',
            'cantidad': [1] * 10 + [0] * 10,
        })
        aptas, no_aptas, dias = seleccionar_categorias_aptas(df, min_dias_venta=15)
        self.assertEqual(dias['esmalte'], 10)
        self.assertEqual(no_aptas, ['esmalte'])
