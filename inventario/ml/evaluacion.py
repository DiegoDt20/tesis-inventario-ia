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

Exactitud del pronóstico (comando evaluar_predicciones): WAPE, exactitud =
1 - WAPE, acierto dentro de tolerancia relativa y R² dentro de cada
categoría, para el modelo y para tres líneas base (media por categoría del
entrenamiento, media móvil de 7 días y último valor) sobre el mismo test.
"""
import numpy as np
import pandas as pd
from django.conf import settings
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


def predecir_con_modelo(modelo, df_features, columnas=COLUMNAS_FEATURES):
    """Genera predicciones de un XGBRegressor sobre un dataset con las
    columnas indicadas (por defecto COLUMNAS_FEATURES)."""
    return modelo.predict(df_features[columnas])


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


# --- Exactitud del pronóstico -----------------------------------------------

def calcular_wape(y_real, y_predicho):
    """WAPE (Weighted Absolute Percentage Error), en fracción:

        WAPE = Σ |real - pronóstico| / Σ real

    Devuelve None si Σ real == 0: el error relativo al volumen no está
    definido cuando no hubo volumen."""
    y_real = np.asarray(y_real, dtype=float)
    y_predicho = np.asarray(y_predicho, dtype=float)
    suma_real = y_real.sum()
    if suma_real == 0:
        return None
    return float(np.abs(y_real - y_predicho).sum() / suma_real)


def calcular_exactitud(wape):
    """Exactitud = 1 - WAPE. Sin truncar a 0: cuando el error supera a la
    demanda real (baja rotación) sale negativa y así se reporta."""
    return None if wape is None else 1 - wape


def acierto_tolerancia_relativa(y_real, y_predicho, tolerancia):
    """Fracción de días con |real - pronóstico| / max(real, 1) <= tolerancia.
    El max(real, 1) evita dividir por cero en días sin demanda: ese día el
    error se mide en unidades. None si no hay días."""
    y_real = np.asarray(y_real, dtype=float)
    y_predicho = np.asarray(y_predicho, dtype=float)
    if len(y_real) == 0:
        return None
    error_relativo = np.abs(y_real - y_predicho) / np.maximum(y_real, 1)
    return float(np.mean(error_relativo <= tolerancia))


def calcular_r2(y_real, y_predicho):
    """R², o None si no está definido (menos de dos días o demanda real
    constante: sin varianza no hay movimiento que explicar)."""
    y_real = np.asarray(y_real, dtype=float)
    if len(y_real) < 2 or np.var(y_real) == 0:
        return None
    return float(r2_score(y_real, y_predicho))


def r2_por_categoria(y_real, y_predicho, categorias):
    """R² calculado dentro de cada categoría por separado ({categoria: R²}).

    El R² global (sobre todas las filas juntas) queda inflado cuando las
    categorías tienen niveles muy distintos: la varianza ENTRE categorías
    domina y el R² mide sobre todo la capacidad de distinguir categorías,
    no la de predecir el movimiento día a día de cada una."""
    df = pd.DataFrame({
        'real': np.asarray(y_real, dtype=float),
        'prediccion': np.asarray(y_predicho, dtype=float),
        'categoria': np.asarray(categorias),
    })
    return {
        str(categoria): calcular_r2(grupo['real'], grupo['prediccion'])
        for categoria, grupo in df.groupby('categoria', sort=True)
    }


def _metricas_exactitud(real, prediccion, tolerancia_relativa):
    wape = calcular_wape(real, prediccion)
    return {
        'dias': int(len(real)),
        'demanda_real': float(np.sum(real)),
        'demanda_pronosticada': float(np.sum(prediccion)),
        'mae': float(mean_absolute_error(real, prediccion)) if len(real) else None,
        'rmse': float(np.sqrt(mean_squared_error(real, prediccion))) if len(real) else None,
        'wape': wape,
        'exactitud': calcular_exactitud(wape),
        'acierto_tolerancia': acierto_tolerancia_relativa(real, prediccion, tolerancia_relativa),
    }


def evaluar_exactitud(y_real, y_predicho, categorias, tolerancia_relativa=None):
    """Métricas de exactitud por categoría y en total.

    Devuelve {'categorias': {categoria: métricas}, 'total': métricas,
    'tolerancia_relativa': t}. Por categoría: días, demanda real y
    pronosticada (sumas), MAE, RMSE, WAPE, exactitud, acierto dentro de
    tolerancia y 'r2' (R² intra-categoría). En el total:

    - WAPE/exactitud son el WAPE AGREGADO sobre las filas de todas las
      categorías con Σ real > 0 (no el promedio de los WAPE por categoría).
      Una categoría con Σ real == 0 tiene WAPE None y no entra en ese total.
    - 'r2_global' es el R² sobre todas las filas juntas (el que se reportaba
      hasta ahora) y 'r2_por_categoria' el dict de R² intra-categoría.
    - MAE, RMSE, demandas y acierto son sobre todas las filas.

    La tolerancia sale de settings.PRONOSTICO_TOLERANCIA_RELATIVA si no se
    pasa."""
    if tolerancia_relativa is None:
        tolerancia_relativa = settings.PRONOSTICO_TOLERANCIA_RELATIVA
    df = pd.DataFrame({
        'real': np.asarray(y_real, dtype=float),
        'prediccion': np.asarray(y_predicho, dtype=float),
        'categoria': np.asarray(categorias).astype(str),
    })

    por_categoria = {}
    for categoria, grupo in df.groupby('categoria', sort=True):
        metricas = _metricas_exactitud(grupo['real'], grupo['prediccion'], tolerancia_relativa)
        metricas['r2'] = calcular_r2(grupo['real'], grupo['prediccion'])
        por_categoria[categoria] = metricas

    total = _metricas_exactitud(df['real'], df['prediccion'], tolerancia_relativa)
    con_volumen = [c for c, m in por_categoria.items() if m['wape'] is not None]
    filas_con_volumen = df[df['categoria'].isin(con_volumen)]
    total['wape'] = calcular_wape(filas_con_volumen['real'], filas_con_volumen['prediccion'])
    total['exactitud'] = calcular_exactitud(total['wape'])
    total['categorias_en_wape'] = con_volumen
    total['r2_global'] = calcular_r2(df['real'], df['prediccion'])
    total['r2_por_categoria'] = {c: m['r2'] for c, m in por_categoria.items()}

    return {'categorias': por_categoria, 'total': total, 'tolerancia_relativa': tolerancia_relativa}


# --- Líneas base de la exactitud ----------------------------------------------
#
# Las tres reciben el train y el test de dividir_temporal (mismo DataFrame
# de origen, índices sin repetir) y devuelven una Serie alineada con
# test.index: se evalúan exactamente sobre las mismas filas que el modelo.

def linea_base_media_categoria(train, test):
    """Media de la demanda diaria de cada serie (categoría) en los días de
    ENTRENAMIENTO, repetida para todos los días del test. Ningún día del test
    entra en el cálculo. Es la referencia natural del R² intra-categoría:
    predecir siempre el nivel medio de la categoría."""
    medias = train.groupby('serie_id')['cantidad'].mean()
    return test['serie_id'].map(medias).fillna(0).astype(float)


def linea_base_media_movil_7(train, test):
    """Media de los 7 días anteriores de la misma serie, encadenada día a
    día: el pronóstico del día t usa la demanda real de t-7 a t-1 (igual
    información que tiene el modelo, cuyas variables de rezago y media
    móvil del día t también usan la demanda real hasta t-1). Nunca usa el
    propio día ni días posteriores."""
    historico = pd.concat([train, test]).sort_values(['serie_id', 'fecha'])
    prediccion = historico.groupby('serie_id')['cantidad'].transform(
        lambda s: s.shift(1).rolling(window=7, min_periods=1).mean()
    )
    return prediccion.loc[test.index].fillna(0)


LINEAS_BASE = {
    'media_categoria': ('Media por categoría (entrenamiento)', linea_base_media_categoria),
    'media_movil_7': ('Media móvil 7 días', linea_base_media_movil_7),
    'ultimo_valor': ('Último valor (día anterior)', linea_base_ingenua),
}


def comparar_metodos(predicciones, tolerancia_relativa=None):
    """Evalúa el modelo y las tres LINEAS_BASE con las mismas métricas
    (evaluar_exactitud). `predicciones` tiene una fila por pronóstico, con
    'categoria', 'real' y una columna por método ('modelo' y cada clave de
    LINEAS_BASE); las filas pueden ser días o ventanas.

    Devuelve {<método>: evaluar_exactitud(...), 'mejor_linea_base': clave de
    la de menor WAPE total, 'supera': {clave: bool}}. El modelo supera a una
    línea base si su WAPE total es menor; solo aporta valor si las supera a
    las tres. Se usa el WAPE porque es la métrica que importa para
    dimensionar pedidos (error relativo al volumen)."""
    resultado = {
        clave: evaluar_exactitud(
            predicciones['real'], predicciones[clave], predicciones['categoria'], tolerancia_relativa,
        )
        for clave in ['modelo', *LINEAS_BASE]
    }

    def wape_total(clave):
        wape = resultado[clave]['total']['wape']
        return np.inf if wape is None else wape

    resultado['mejor_linea_base'] = min(LINEAS_BASE, key=wape_total)
    resultado['supera'] = {clave: wape_total('modelo') < wape_total(clave) for clave in LINEAS_BASE}
    return resultado


def comparar_exactitud(train, test, y_modelo, tolerancia_relativa=None):
    """División única: evalúa el modelo (predicciones y_modelo, alineadas
    con test) y las tres LINEAS_BASE sobre el mismo test (ver
    comparar_metodos). La categoría de cada fila es su serie_id (modelo a
    nivel de categoría). Además de lo que devuelve comparar_metodos incluye
    'predicciones', el DataFrame fila a fila (fecha, categoria, real y cada
    pronóstico)."""
    predicciones = pd.DataFrame({
        'fecha': test['fecha'].to_numpy(),
        'categoria': test['serie_id'].astype(str).to_numpy(),
        'real': test['cantidad'].to_numpy(dtype=float),
        'modelo': np.asarray(y_modelo, dtype=float),
    })
    for clave, (_, funcion) in LINEAS_BASE.items():
        predicciones[clave] = funcion(train, test).to_numpy(dtype=float)

    resultado = comparar_metodos(predicciones, tolerancia_relativa)
    resultado['predicciones'] = predicciones
    return resultado
