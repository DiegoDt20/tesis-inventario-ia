"""Carga el dataset externo de preentrenamiento (Store Item Demand
Forecasting Challenge, Kaggle) y lo deja en el formato que espera
construir_features: columnas 'fecha', 'serie_id' y 'cantidad'.

Solo se usan fecha, tienda, producto y cantidad vendida: el dataset externo
no tiene precio, stock ni ninguna otra variable, así que ese es exactamente
el conjunto de datos disponible para derivar variables en ambas fases.
"""
import pandas as pd

RUTA_POR_DEFECTO = 'datos/train.csv'


def leer_datos_externos(ruta=RUTA_POR_DEFECTO):
    """Lee el CSV externo y arma serie_id combinando tienda (store) y
    producto (item), ya que el dataset original trae una serie de tiempo
    por cada combinación tienda-producto."""
    df = pd.read_csv(ruta, parse_dates=['date'])
    df['serie_id'] = df['store'].astype(str) + '-' + df['item'].astype(str)
    df = df.rename(columns={'date': 'fecha', 'sales': 'cantidad'})
    return df[['fecha', 'serie_id', 'cantidad']]
