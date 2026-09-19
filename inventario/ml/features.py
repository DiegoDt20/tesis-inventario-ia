"""Construcción de variables (features) para el motor de predicción de demanda.

Esta misma función se usa tanto para el dataset externo de preentrenamiento
(Kaggle) como para los datos internos de la microempresa: el conjunto de
variables debe ser IDÉNTICO en ambas fases para que la transferencia de
aprendizaje funcione. Por eso solo se derivan variables a partir de fecha,
serie y cantidad, que es lo único que el dataset externo tiene.
"""
import pandas as pd

# Columnas de entrada (X) que usa el modelo, en el orden en que se le pasan.
# Debe mantenerse igual entre carga_externa, carga_interna y entrenamiento.
COLUMNAS_FEATURES = [
    'dia_semana', 'dia_mes', 'mes', 'semana_anio', 'es_fin_de_semana',
    'rezago_7', 'rezago_14', 'rezago_30',
    'media_movil_7', 'media_movil_30', 'std_movil_30',
]

COLUMNAS_SALIDA = ['fecha', 'serie_id', 'cantidad'] + COLUMNAS_FEATURES


def _densificar(df):
    """Reindexa cada serie al rango diario completo entre su primera y su
    última fecha, para que los días sin ventas/pedidos queden como cantidad
    cero en vez de simplemente no existir (huecos romperían los rezagos:
    un rezago de "7 días" dejaría de corresponder a 7 días calendario)."""
    piezas = []
    for serie_id, grupo in df.groupby('serie_id', sort=False):
        grupo = grupo.set_index('fecha').sort_index()
        rango = pd.date_range(grupo.index.min(), grupo.index.max(), freq='D')
        grupo = grupo.reindex(rango)
        grupo['serie_id'] = serie_id
        grupo['cantidad'] = grupo['cantidad'].fillna(0)
        grupo.index.name = 'fecha'
        piezas.append(grupo.reset_index())
    return pd.concat(piezas, ignore_index=True)


def construir_features(df):
    """Recibe un DataFrame con columnas 'fecha', 'serie_id' y 'cantidad'
    (una fila por combinación fecha/serie; puede venir con huecos) y
    devuelve el dataset de entrenamiento con COLUMNAS_SALIDA.

    Todas las variables derivadas (rezagos y medias/desviación móviles) se
    calculan usando únicamente información de días ANTERIORES al día de la
    fila: nunca se filtra el valor del propio día ni de días futuros.
    """
    df = df[['fecha', 'serie_id', 'cantidad']].copy()
    df['fecha'] = pd.to_datetime(df['fecha'])
    # Por si la fuente trae más de una fila para la misma fecha/serie.
    df = df.groupby(['serie_id', 'fecha'], as_index=False)['cantidad'].sum()
    df = _densificar(df)
    df = df.sort_values(['serie_id', 'fecha']).reset_index(drop=True)

    df['dia_semana'] = df['fecha'].dt.dayofweek
    df['dia_mes'] = df['fecha'].dt.day
    df['mes'] = df['fecha'].dt.month
    df['semana_anio'] = df['fecha'].dt.isocalendar().week.astype(int)
    df['es_fin_de_semana'] = (df['dia_semana'] >= 5).astype(int)

    cantidad_por_serie = df.groupby('serie_id')['cantidad']
    df['rezago_7'] = cantidad_por_serie.shift(7)
    df['rezago_14'] = cantidad_por_serie.shift(14)
    df['rezago_30'] = cantidad_por_serie.shift(30)

    # shift(1) antes de la ventana: la media/desviación del día t solo puede
    # ver cantidades hasta t-1, nunca la del propio día t (eso sería fuga).
    df['media_movil_7'] = cantidad_por_serie.transform(
        lambda s: s.shift(1).rolling(window=7, min_periods=1).mean()
    )
    df['media_movil_30'] = cantidad_por_serie.transform(
        lambda s: s.shift(1).rolling(window=30, min_periods=1).mean()
    )
    df['std_movil_30'] = cantidad_por_serie.transform(
        lambda s: s.shift(1).rolling(window=30, min_periods=1).std()
    )

    return df[COLUMNAS_SALIDA]


def dividir_temporal(df, dias_test=30):
    """Divide un dataset ya construido con construir_features en train/test
    respetando el orden cronológico: los últimos `dias_test` días (según la
    fecha máxima del propio DataFrame) son el conjunto de test.

    Nunca usar train_test_split con shuffle en series de tiempo: mezclar
    filtra el futuro al pasado y las métricas de evaluación salen infladas.
    """
    fecha_corte = df['fecha'].max() - pd.Timedelta(days=dias_test - 1)
    train = df[df['fecha'] < fecha_corte].copy()
    test = df[df['fecha'] >= fecha_corte].copy()
    return train, test
