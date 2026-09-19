"""Evaluación del motor de predicción de demanda.

Calcula MAE, RMSE, SMAPE y R², y compara tres modelos sobre el MISMO
conjunto de test (a nivel de categoría cuando el ajuste se entrenó con
--nivel categoria), para poder demostrar si la transferencia de aprendizaje
aporta valor frente a entrenar solo con lo poco que tiene la microempresa:

  a) línea base ingenua: predecir la demanda del día anterior.
  b) modelo entrenado solo con datos internos (desde cero, sin transferencia).
  c) modelo preentrenado con el dataset externo y ajustado con datos internos.

Se usa SMAPE en vez de MAPE: con demanda real en cero (muy frecuente en
productos/categorías de baja rotación) el MAPE da valores astronómicos o
indefinidos (división por cero), mientras que SMAPE está acotado.
"""
import numpy as np
import pandas as pd
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

from .entrenamiento import HIPERPARAMETROS_BASE, entrenar_modelo
from .features import COLUMNAS_FEATURES


def calcular_smape(y_real, y_predicho):
    """SMAPE (Symmetric Mean Absolute Percentage Error), en fracción (no en
    porcentaje). Cuando la demanda real y la predicha son ambas cero el
    término se define como 0 en vez de indeterminado (0/0)."""
    y_real = np.asarray(y_real, dtype=float)
    y_predicho = np.asarray(y_predicho, dtype=float)
    numerador = np.abs(y_predicho - y_real)
    denominador = np.abs(y_real) + np.abs(y_predicho)
    terminos = np.divide(
        numerador, denominador, out=np.zeros_like(numerador), where=denominador != 0,
    )
    return float(np.mean(terminos) * 2)


def porcentaje_demanda_cero(y_real):
    """Fracción (en porcentaje) de filas con demanda real igual a cero,
    para poder interpretar el SMAPE: con muchas filas en cero, cualquier
    métrica de error relativo pierde fuerza frente al MAE/RMSE."""
    y_real = np.asarray(y_real, dtype=float)
    if len(y_real) == 0:
        return 0.0
    return float(np.mean(y_real == 0) * 100)


def calcular_metricas(y_real, y_predicho):
    """Calcula MAE, RMSE, SMAPE y R² para un conjunto de predicciones."""
    return {
        'mae': float(mean_absolute_error(y_real, y_predicho)),
        'rmse': float(np.sqrt(mean_squared_error(y_real, y_predicho))),
        'smape': calcular_smape(y_real, y_predicho),
        'r2': float(r2_score(y_real, y_predicho)),
    }


def predecir_con_modelo(modelo, df_features):
    """Genera predicciones de un XGBRegressor sobre un dataset con las
    columnas de COLUMNAS_FEATURES."""
    return modelo.predict(df_features[COLUMNAS_FEATURES])


def linea_base_ingenua(train, test):
    """Línea base ingenua: la demanda de cada día se predice igual a la del
    día anterior de esa misma serie. Usa train+test concatenados solo para
    poder mirar hacia atrás en la primera fecha de test (nunca hacia
    adelante: cada predicción de test solo usa un día estrictamente
    anterior de la propia serie)."""
    historico = pd.concat([train, test]).sort_values(['serie_id', 'fecha'])
    prediccion = historico.groupby('serie_id')['cantidad'].shift(1)
    return prediccion.loc[test.index].fillna(0)


def comparar_modelos(train_interno, test_interno, modelo_ajustado):
    """Evalúa los tres modelos sobre test_interno y devuelve un dict con
    las métricas de cada uno ('linea_base', 'solo_interno' y 'ajustado') más
    'pct_demanda_cero', el porcentaje de filas de test con demanda real
    cero (mismo test para los tres, así que el porcentaje es único)."""
    y_real = test_interno['cantidad']

    y_base = linea_base_ingenua(train_interno, test_interno)
    metricas_linea_base = calcular_metricas(y_real, y_base)

    modelo_solo_interno = entrenar_modelo(train_interno, HIPERPARAMETROS_BASE)
    y_solo_interno = predecir_con_modelo(modelo_solo_interno, test_interno)
    metricas_solo_interno = calcular_metricas(y_real, y_solo_interno)

    y_ajustado = predecir_con_modelo(modelo_ajustado, test_interno)
    metricas_ajustado = calcular_metricas(y_real, y_ajustado)

    return {
        'linea_base': metricas_linea_base,
        'solo_interno': metricas_solo_interno,
        'ajustado': metricas_ajustado,
        'pct_demanda_cero': porcentaje_demanda_cero(y_real),
    }
