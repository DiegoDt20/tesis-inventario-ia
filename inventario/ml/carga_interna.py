"""Carga la demanda histórica interna de la microempresa para el ajuste
(fase 2) del motor de predicción.

Se lee desde PedidoDetalle, no desde Movimiento, y se usa
cantidad_solicitada, no cantidad_atendida: la demanda real es lo que el
cliente pidió, no lo que se le pudo entregar. Entrenar con lo atendido
haría que el modelo aprenda una demanda subestimada y perpetúe el
desabastecimiento que ya sufre el negocio.

Con el volumen actual de ventas ningún producto individual llega a
suficientes días con venta al mes como para entrenar y predecir su demanda
uno por uno, así que también se ofrece una carga agrupada por categoría
(leer_demanda_interna_por_categoria) y una función para decidir qué
categorías tienen histórico suficiente (seleccionar_categorias_aptas).
"""
import pandas as pd

from inventario.models import PedidoDetalle

MIN_DIAS_VENTA_CATEGORIA = 15


def leer_demanda_interna(origen=None):
    """Devuelve un DataFrame con columnas 'fecha', 'serie_id' (id del
    producto) y 'cantidad' (suma de cantidad_solicitada por producto y
    fecha del pedido). Los días sin pedidos simplemente no aparecen aquí;
    construir_features se encarga de completarlos con cantidad cero."""
    consulta = PedidoDetalle.objects.select_related('pedido')
    if origen:
        consulta = consulta.filter(pedido__origen=origen)

    filas = list(consulta.values('producto_id', 'pedido__fecha_solicitud', 'cantidad_solicitada'))
    if not filas:
        return pd.DataFrame(columns=['fecha', 'serie_id', 'cantidad'])

    df = pd.DataFrame.from_records(filas)
    df['fecha'] = pd.to_datetime(df['pedido__fecha_solicitud']).dt.date
    df['fecha'] = pd.to_datetime(df['fecha'])
    df = df.rename(columns={'producto_id': 'serie_id', 'cantidad_solicitada': 'cantidad'})
    df = df.groupby(['serie_id', 'fecha'], as_index=False)['cantidad'].sum()
    return df[['fecha', 'serie_id', 'cantidad']]


def leer_demanda_interna_por_categoria(origen=None):
    """Igual que leer_demanda_interna, pero agrupa por la categoría del
    producto (Producto.categoria) en vez de por producto individual:
    serie_id pasa a ser la categoría. El resto del pipeline (construir_features,
    dividir_temporal, entrenamiento) no cambia: solo cambia qué significa
    cada serie."""
    consulta = PedidoDetalle.objects.select_related('pedido', 'producto')
    if origen:
        consulta = consulta.filter(pedido__origen=origen)

    filas = list(consulta.values('producto__categoria', 'pedido__fecha_solicitud', 'cantidad_solicitada'))
    if not filas:
        return pd.DataFrame(columns=['fecha', 'serie_id', 'cantidad'])

    df = pd.DataFrame.from_records(filas)
    df['fecha'] = pd.to_datetime(df['pedido__fecha_solicitud']).dt.date
    df['fecha'] = pd.to_datetime(df['fecha'])
    df = df.rename(columns={'producto__categoria': 'serie_id', 'cantidad_solicitada': 'cantidad'})
    df = df.groupby(['serie_id', 'fecha'], as_index=False)['cantidad'].sum()
    return df[['fecha', 'serie_id', 'cantidad']]


def dias_con_venta_por_serie(df):
    """Cuenta, por serie_id, la cantidad de fechas distintas con al menos
    una venta (cantidad > 0) en el histórico disponible. Se calcula sobre
    las filas ya agregadas por fecha/serie (antes de densificar a diario),
    así que cada fila representa un día con pedidos reales."""
    ventas = df[df['cantidad'] > 0]
    return ventas.groupby('serie_id')['fecha'].nunique().to_dict()


def seleccionar_categorias_aptas(df, min_dias_venta=MIN_DIAS_VENTA_CATEGORIA):
    """Divide las categorías presentes en `df` (salida de
    leer_demanda_interna_por_categoria) entre aptas (>= min_dias_venta días
    con venta en el histórico) y no aptas. Las no aptas no tienen historial
    suficiente para entrenar ni predecir con el motor de ML: se gestionan
    solo con el punto de reorden del motor de decisiones.

    Devuelve (aptas, no_aptas, dias_por_categoria): aptas/no_aptas son listas
    de nombres de categoría ordenadas alfabéticamente, y dias_por_categoria
    es un dict categoria -> días con venta."""
    dias_por_categoria = dias_con_venta_por_serie(df)
    aptas = sorted(c for c, dias in dias_por_categoria.items() if dias >= min_dias_venta)
    no_aptas = sorted(c for c, dias in dias_por_categoria.items() if dias < min_dias_venta)
    return aptas, no_aptas, dias_por_categoria
