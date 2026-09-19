"""Desagregación de predicciones de categoría a nivel de producto.

Cuando el modelo activo predice a nivel de categoría (ver
inventario/ml/carga_interna.py), el motor de decisiones sigue necesitando
una demanda predicha por producto. Este módulo reparte la demanda predicha
de cada categoría entre sus productos según la participación histórica de
cada uno (unidades del producto / unidades de la categoría), para que
Prediccion se siga guardando a nivel de producto sin tocar el motor de
decisiones (inventario/decisiones/).
"""
import pandas as pd

from inventario.models import PedidoDetalle, Producto


def calcular_participacion(origen=None):
    """Devuelve un DataFrame con columnas 'producto_id', 'categoria' y
    'participacion': la fracción de unidades vendidas históricamente por
    cada producto dentro de su propia categoría. Incluye TODOS los
    productos activos, aunque no tengan historial: un producto sin ventas
    registradas recibe participación 0.0 en vez de fallar (no se puede
    dividir por cero unidades)."""
    productos_todos = pd.DataFrame(
        list(Producto.objects.values('id', 'categoria'))
    ).rename(columns={'id': 'producto_id'})
    if productos_todos.empty:
        return pd.DataFrame(columns=['producto_id', 'categoria', 'participacion'])

    consulta = PedidoDetalle.objects.select_related('pedido', 'producto')
    if origen:
        consulta = consulta.filter(pedido__origen=origen)
    filas = list(consulta.values('producto_id', 'producto__categoria', 'cantidad_solicitada'))

    if not filas:
        participacion = productos_todos.copy()
        participacion['participacion'] = 0.0
        return participacion[['producto_id', 'categoria', 'participacion']]

    ventas = pd.DataFrame.from_records(filas).rename(columns={'producto__categoria': 'categoria'})
    unidades_producto = ventas.groupby(
        ['producto_id', 'categoria'], as_index=False,
    )['cantidad_solicitada'].sum()
    unidades_categoria = unidades_producto.groupby('categoria')['cantidad_solicitada'].transform('sum')
    unidades_producto['participacion'] = (
        unidades_producto['cantidad_solicitada'] / unidades_categoria
    ).fillna(0.0)

    participacion = productos_todos.merge(
        unidades_producto[['producto_id', 'participacion']], on='producto_id', how='left',
    )
    participacion['participacion'] = participacion['participacion'].fillna(0.0)
    return participacion[['producto_id', 'categoria', 'participacion']]


def desagregar_prediccion_categoria(categoria, demanda_predicha_categoria, participacion):
    """Reparte `demanda_predicha_categoria` (un único valor de demanda
    predicha para una categoría, en un día objetivo) entre los productos de
    esa categoría según su participación histórica.

    Devuelve una lista de tuplas (producto_id, demanda_producto,
    participacion_usada), una por cada producto de la categoría, incluyendo
    los de participación 0."""
    productos_categoria = participacion[participacion['categoria'] == categoria]
    return [
        (
            int(fila.producto_id),
            demanda_predicha_categoria * fila.participacion,
            float(fila.participacion),
        )
        for fila in productos_categoria.itertuples()
    ]
