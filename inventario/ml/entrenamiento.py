"""Entrenamiento del motor de predicción de demanda, en dos fases:

Fase 1 (base): entrena un XGBRegressor desde cero sobre el dataset externo
de Kaggle (preentrenamiento por transferencia).

Fase 2 (ajustado): continúa el entrenamiento del modelo base sobre los
datos internos de la microempresa, usando el parámetro xgb_model de
XGBRegressor.fit para partir de los árboles ya aprendidos. Usa menos
árboles y un learning rate más bajo que la fase base para no borrar lo
aprendido en el preentrenamiento.

En ambas fases la división train/test es TEMPORAL (ver features.dividir_temporal):
nunca se usa un split aleatorio, porque en series de tiempo eso filtraría
el futuro al pasado e inflaría las métricas.
"""
from pathlib import Path

import xgboost as xgb

from .features import COLUMNAS_FEATURES, construir_features, dividir_temporal

DIR_MODELOS = Path('modelos')

# Fase 1: entrena desde cero, con más árboles y un learning rate normal.
HIPERPARAMETROS_BASE = {
    'n_estimators': 300,
    'learning_rate': 0.05,
    'max_depth': 6,
    'subsample': 0.8,
    'colsample_bytree': 0.8,
    'random_state': 42,
}

# Fase 2: menos árboles y learning rate más bajo para ajustar sin borrar lo
# aprendido en la fase base.
HIPERPARAMETROS_AJUSTE = {
    'n_estimators': 50,
    'learning_rate': 0.01,
    'max_depth': 6,
    'subsample': 0.8,
    'colsample_bytree': 0.8,
    'random_state': 42,
}


def entrenar_modelo(df_train, hiperparametros, xgb_model=None):
    """Entrena un XGBRegressor sobre df_train (ya con las columnas de
    COLUMNAS_FEATURES y 'cantidad' como objetivo). Si se pasa xgb_model
    (ruta a un modelo ya entrenado), continúa el entrenamiento a partir de
    esos árboles en vez de empezar desde cero."""
    X = df_train[COLUMNAS_FEATURES]
    y = df_train['cantidad']
    modelo = xgb.XGBRegressor(**hiperparametros)
    modelo.fit(X, y, xgb_model=xgb_model)
    return modelo


def guardar_modelo(modelo, nombre_archivo):
    """Guarda el modelo en DIR_MODELOS y devuelve la ruta como string."""
    DIR_MODELOS.mkdir(parents=True, exist_ok=True)
    ruta = DIR_MODELOS / nombre_archivo
    modelo.save_model(str(ruta))
    return str(ruta)


def entrenar_fase_base(df_externo, dias_test=30):
    """Fase 1: construye las features del dataset externo, separa el
    conjunto de test temporal y entrena el modelo base desde cero.
    Devuelve (modelo, train, test, hiperparametros)."""
    df_features = construir_features(df_externo)
    train, test = dividir_temporal(df_features, dias_test=dias_test)
    modelo = entrenar_modelo(train, HIPERPARAMETROS_BASE)
    return modelo, train, test, HIPERPARAMETROS_BASE


def entrenar_fase_ajuste(df_interno, ruta_modelo_base, dias_test=30):
    """Fase 2: construye las features de los datos internos, separa el
    conjunto de test temporal y continúa el entrenamiento del modelo base
    (ruta_modelo_base) sobre el conjunto de train interno.
    Devuelve (modelo, train, test, hiperparametros)."""
    df_features = construir_features(df_interno)
    train, test = dividir_temporal(df_features, dias_test=dias_test)
    modelo = entrenar_modelo(train, HIPERPARAMETROS_AJUSTE, xgb_model=ruta_modelo_base)
    return modelo, train, test, HIPERPARAMETROS_AJUSTE
