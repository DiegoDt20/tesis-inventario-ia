"""Optimización del motor de predicción de demanda (comando optimizar_modelo).

Prueba configuraciones alternativas del modelo preentrenado + ajustado SIN
tocar el modelo activo:

  1. Búsqueda aleatoria de hiperparámetros de la fase de ajuste, con
     validación cruzada TEMPORAL (TimeSeriesSplit) sobre los días de
     entrenamiento. El conjunto de prueba nunca participa de la búsqueda.
  2. Variantes de la fase base: número de árboles y learning rate del
     preentrenamiento con el dataset externo.
  3. Variantes del conjunto de variables, cada una por separado. Como la
     regla del proyecto exige que las variables sean IDÉNTICAS entre
     preentrenamiento y ajuste, cada variante reentrena también la fase
     base con ese mismo conjunto de variables (no se puede continuar un
     modelo base entrenado con otras columnas).

Todas las configuraciones se miden sobre el MISMO conjunto de prueba (los
últimos `dias_test` días, separados con dividir_temporal), que solo sirve
para medir. Para elegir una configuración sin mirar el test se reporta
también el R² de validación cruzada fuera de pliegue (sobre los días de
entrenamiento): con un test tan chico, elegir "la de mayor R² en test" es
volver a ajustar al test.

Además del R² a un paso (las variables de cada día de test usan la demanda
real de los días anteriores, como en entrenar_ajustado) se reporta el R²
recursivo: el pronóstico de todo el periodo de prueba hecho desde el último
día de entrenamiento, alimentando cada predicción como historial de la
siguiente, que es como predecir_demanda usa el modelo en producción.
"""
import itertools

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.model_selection import ParameterSampler, TimeSeriesSplit

from .entrenamiento import HIPERPARAMETROS_AJUSTE, HIPERPARAMETROS_BASE, entrenar_modelo
from .evaluacion import calcular_metricas, linea_base_ingenua, predecir_con_modelo
from .features import COLUMNAS_FEATURES, construir_features, dividir_temporal

# Espacio de búsqueda de la fase de ajuste. Se usan listas (no
# distribuciones continuas) para que la búsqueda sea reproducible y legible
# en la tesis. n_estimators son árboles NUEVOS que se agregan sobre los del
# modelo base.
ESPACIO_AJUSTE = {
    'n_estimators': [10, 25, 50, 100, 200, 400],
    'learning_rate': [0.005, 0.01, 0.02, 0.05, 0.1, 0.2],
    'max_depth': [2, 3, 4, 6, 8],
    'min_child_weight': [1, 2, 3, 5, 10],
    'subsample': [0.5, 0.7, 0.8, 1.0],
    'colsample_bytree': [0.5, 0.7, 0.8, 1.0],
    'reg_lambda': [0, 0.5, 1, 5, 10, 50],
}

# Variantes de la fase base: combinaciones de árboles y learning rate del
# preentrenamiento. La combinación de HIPERPARAMETROS_BASE se omite porque
# ya es la configuración actual.
ARBOLES_BASE = [150, 300, 600]
LEARNING_RATES_BASE = [0.02, 0.05, 0.1]

# Días finales del dataset externo que se dejan fuera del preentrenamiento,
# igual que entrenar_base (--dias-test 30).
DIAS_TEST_EXTERNO = 30

GRUPO_REFERENCIA = 'Referencia'
GRUPO_AJUSTE = '1. Hiperparámetros del ajuste'
GRUPO_BASE = '2. Fase base'
GRUPO_VARIABLES = '3. Variables'
GRUPO_COMBINADA = '4. Combinada'


def particiones_por_fecha(train, n_pliegues):
    """TimeSeriesSplit sobre las FECHAS de train, no sobre las filas: todas
    las series de un mismo día caen siempre en el mismo pliegue, y cada
    pliegue de validación es posterior a todo su pliegue de entrenamiento.
    Devuelve una lista de (índices_entrenamiento, índices_validación) con
    etiquetas del índice de `train`."""
    fechas = np.sort(train['fecha'].unique())
    particiones = []
    for pos_entrenamiento, pos_validacion in TimeSeriesSplit(n_splits=n_pliegues).split(fechas):
        en_entrenamiento = train['fecha'].isin(fechas[pos_entrenamiento])
        en_validacion = train['fecha'].isin(fechas[pos_validacion])
        particiones.append((train.index[en_entrenamiento], train.index[en_validacion]))
    return particiones


def metricas_validacion_cruzada(train, particiones, predecir_pliegue):
    """Calcula las métricas fuera de pliegue: `predecir_pliegue(df_ent,
    df_val)` entrena con df_ent y devuelve las predicciones para df_val. Se
    juntan las predicciones de todos los pliegues y se calculan las métricas
    una sola vez (más estable que promediar el R² de pliegues de pocos días;
    como todas las configuraciones se validan sobre las mismas filas,
    maximizar este R² equivale a minimizar el error cuadrático)."""
    reales, predichos = [], []
    for idx_entrenamiento, idx_validacion in particiones:
        df_validacion = train.loc[idx_validacion]
        predichos.append(np.asarray(predecir_pliegue(train.loc[idx_entrenamiento], df_validacion)))
        reales.append(df_validacion['cantidad'].to_numpy())
    return calcular_metricas(np.concatenate(reales), np.concatenate(predichos))


def _con_semilla(hiperparametros, semilla):
    return {**hiperparametros, 'random_state': semilla}


def ajustar(train, booster_base, hiperparametros, columnas):
    """Fase 2 con un modelo base en memoria (no se modifica: XGBoost copia
    el Booster antes de agregarle árboles)."""
    return entrenar_modelo(train, hiperparametros, xgb_model=booster_base, columnas=columnas)


def cv_ajuste(train, particiones, booster_base, hiperparametros, columnas):
    """Validación cruzada temporal del ajuste: en cada pliegue se parte del
    MISMO modelo base y se ajusta solo con los días de ese pliegue."""
    def predecir_pliegue(df_entrenamiento, df_validacion):
        modelo = ajustar(df_entrenamiento, booster_base, hiperparametros, columnas)
        return predecir_con_modelo(modelo, df_validacion, columnas)
    return metricas_validacion_cruzada(train, particiones, predecir_pliegue)


def buscar_hiperparametros_ajuste(train, particiones, booster_base, columnas, n_iteraciones, semilla):
    """Búsqueda aleatoria (ParameterSampler, equivalente a RandomizedSearchCV)
    de los hiperparámetros de ajuste, evaluada con cv_ajuste. Se usa un bucle
    propio en vez de RandomizedSearchCV porque cada candidato debe continuar
    el entrenamiento de un modelo base (xgb_model) y validarse con
    particiones por fecha. La configuración actual (HIPERPARAMETROS_AJUSTE)
    siempre entra como candidata, así que la elegida nunca es peor que ella
    en validación cruzada.

    Devuelve la lista de candidatos [{'hiperparametros', 'cv'}] ordenada de
    mejor a peor R² de validación cruzada."""
    candidatos = [_con_semilla(HIPERPARAMETROS_AJUSTE, semilla)]
    candidatos += [
        _con_semilla(params, semilla)
        for params in ParameterSampler(ESPACIO_AJUSTE, n_iter=n_iteraciones, random_state=semilla)
    ]
    resultados = [
        {'hiperparametros': params, 'cv': cv_ajuste(train, particiones, booster_base, params, columnas)}
        for params in candidatos
    ]
    # sorted es estable: ante un empate gana el que entró primero (la actual).
    return sorted(resultados, key=lambda r: -r['cv']['r2'])


def predecir_recursivo(modelo, historial, fechas_objetivo, columnas, experimentales):
    """Pronostica `fechas_objetivo` para todas las series de `historial`
    (fecha, serie_id, cantidad, solo días de entrenamiento) igual que
    predecir_demanda: día por día, cada predicción (con piso en cero) se
    agrega al historial para calcular los rezagos y medias del día
    siguiente. Nunca ve la demanda real del periodo pronosticado.

    Devuelve un DataFrame con fecha, serie_id y prediccion."""
    serie = historial[['fecha', 'serie_id', 'cantidad']].copy()
    series = serie['serie_id'].unique()
    resultados = []
    for fecha in sorted(fechas_objetivo):
        filas_objetivo = pd.DataFrame({'fecha': fecha, 'serie_id': series, 'cantidad': 0.0})
        features = construir_features(
            pd.concat([serie, filas_objetivo], ignore_index=True), experimentales=experimentales,
        )
        fila = features[features['fecha'] == fecha]
        prediccion = np.clip(modelo.predict(fila[columnas]), 0, None)
        nuevas = pd.DataFrame({'fecha': fecha, 'serie_id': fila['serie_id'].to_numpy(), 'cantidad': prediccion})
        serie = pd.concat([serie, nuevas], ignore_index=True)
        resultados.append(nuevas.rename(columns={'cantidad': 'prediccion'}))
    return pd.concat(resultados, ignore_index=True)


def r2_recursivo(predicciones, test):
    """R² del pronóstico recursivo contra la demanda real de test,
    emparejando por fecha y serie."""
    unido = test[['fecha', 'serie_id', 'cantidad']].merge(
        predicciones, on=['fecha', 'serie_id'], how='left', validate='one_to_one',
    )
    return calcular_metricas(unido['cantidad'], unido['prediccion'].fillna(0))['r2']


def importancia_variables(modelo, columnas):
    """Importancia por ganancia (feature_importances_ de XGBoost), de mayor
    a menor, como lista de (variable, importancia)."""
    return sorted(
        zip(columnas, (float(v) for v in modelo.feature_importances_)),
        key=lambda par: -par[1],
    )


class Optimizador:
    """Corre todas las configuraciones sobre una misma división temporal y
    acumula una fila de resultados por configuración.

    df_interno y df_externo tienen columnas fecha, serie_id y cantidad.
    modelo_base_actual es el XGBRegressor base activo (el de la
    configuración vigente); si es None se entrena con HIPERPARAMETROS_BASE.
    `registrar` recibe mensajes de avance (por defecto se descartan)."""

    def __init__(self, df_interno, df_externo, dias_test, n_pliegues=4, n_iteraciones=100,
                 n_quitar=3, semilla=42, modelo_base_actual=None, registrar=None):
        self.dias_test = dias_test
        self.n_iteraciones = n_iteraciones
        self.n_quitar = n_quitar
        self.semilla = semilla
        self.registrar = registrar or (lambda mensaje: None)

        features_internas = construir_features(df_interno, experimentales=True)
        self.train, self.test = dividir_temporal(features_internas, dias_test=dias_test)
        fecha_corte = self.test['fecha'].min()
        # Historial crudo hasta el último día de entrenamiento, para el
        # pronóstico recursivo (sin ningún dato del periodo de prueba).
        self.historial = df_interno[pd.to_datetime(df_interno['fecha']) < fecha_corte].copy()
        self.historial['fecha'] = pd.to_datetime(self.historial['fecha'])
        self.particiones = particiones_por_fecha(self.train, n_pliegues)

        features_externas = construir_features(df_externo, experimentales=True)
        self.train_externo, self.test_externo = dividir_temporal(
            features_externas, dias_test=DIAS_TEST_EXTERNO,
        )
        self.modelo_base_actual = modelo_base_actual
        self._bases = {}
        self.filas = []
        self.candidatos_ajuste = []
        self.importancias = []
        self.variables_quitadas = []
        self.ruido_semilla = []

    # --- Modelos base -------------------------------------------------------

    def modelo_base(self, hiperparametros, columnas):
        """Modelo base entrenado con el dataset externo para esos
        hiperparámetros y variables (se guarda en memoria para reutilizarlo).
        La configuración actual usa el modelo base activo tal cual."""
        clave = (tuple(sorted(hiperparametros.items())), tuple(columnas))
        if clave not in self._bases:
            es_actual = hiperparametros == HIPERPARAMETROS_BASE and list(columnas) == COLUMNAS_FEATURES
            if es_actual and self.modelo_base_actual is not None:
                modelo = self.modelo_base_actual
            else:
                modelo = entrenar_modelo(self.train_externo, hiperparametros, columnas=columnas)
            r2_externo = calcular_metricas(
                self.test_externo['cantidad'], predecir_con_modelo(modelo, self.test_externo, columnas),
            )['r2']
            self.registrar(
                f'  Base n_estimators={hiperparametros["n_estimators"]}, '
                f'learning_rate={hiperparametros["learning_rate"]}, {len(columnas)} variables: '
                f'R² en test externo {r2_externo:.4f}'
            )
            self._bases[clave] = (modelo, r2_externo)
        return self._bases[clave]

    # --- Evaluación ---------------------------------------------------------

    def _agregar(self, nombre, grupo, cv, y_test, prediccion_recursiva, **detalle):
        fila = {
            'nombre': nombre,
            'grupo': grupo,
            'cv': cv,
            'test': calcular_metricas(self.test['cantidad'], y_test),
            'r2_recursivo': r2_recursivo(prediccion_recursiva, self.test),
            **detalle,
        }
        self.filas.append(fila)
        return fila

    def evaluar_ajustado(self, nombre, grupo, hp_base, hp_ajuste, columnas, cv=None):
        """Ajusta sobre los días de entrenamiento completos y mide en test.
        Si no se pasa `cv` (ya calculado en la búsqueda) se calcula aquí."""
        experimentales = any(c not in COLUMNAS_FEATURES for c in columnas)
        modelo_base, r2_externo = self.modelo_base(hp_base, columnas)
        booster = modelo_base.get_booster()
        if cv is None:
            cv = cv_ajuste(self.train, self.particiones, booster, hp_ajuste, columnas)
        modelo = ajustar(self.train, booster, hp_ajuste, columnas)
        return self._agregar(
            nombre, grupo, cv,
            predecir_con_modelo(modelo, self.test, columnas),
            predecir_recursivo(modelo, self.historial, self.fechas_test, columnas, experimentales),
            hiperparametros_base=hp_base, hiperparametros_ajuste=hp_ajuste,
            columnas=list(columnas), r2_externo_base=r2_externo, modelo=modelo,
        )

    @property
    def fechas_test(self):
        return sorted(self.test['fecha'].unique())

    def evaluar_referencias(self):
        """Línea base ingenua, modelo solo con datos internos y modelo base
        sin ajuste: no son candidatas, sirven para ubicar las demás."""
        columnas = COLUMNAS_FEATURES

        cv = metricas_validacion_cruzada(self.train, self.particiones, linea_base_ingenua)
        ultimo_valor = self.historial.sort_values('fecha').groupby('serie_id')['cantidad'].last()
        persistencia = pd.DataFrame(
            [(f, s, v) for f in self.fechas_test for s, v in ultimo_valor.items()],
            columns=['fecha', 'serie_id', 'prediccion'],
        )
        self._agregar(
            'Línea base ingenua (demanda del día anterior)', GRUPO_REFERENCIA, cv,
            linea_base_ingenua(self.train, self.test), persistencia, columnas=[],
        )

        def solo_interno(df_entrenamiento, df_validacion):
            modelo = entrenar_modelo(df_entrenamiento, HIPERPARAMETROS_BASE, columnas=columnas)
            return predecir_con_modelo(modelo, df_validacion, columnas)
        cv = metricas_validacion_cruzada(self.train, self.particiones, solo_interno)
        modelo = entrenar_modelo(self.train, HIPERPARAMETROS_BASE, columnas=columnas)
        self._agregar(
            'Solo datos internos (sin transferencia)', GRUPO_REFERENCIA, cv,
            predecir_con_modelo(modelo, self.test, columnas),
            predecir_recursivo(modelo, self.historial, self.fechas_test, columnas, False),
            hiperparametros_base=HIPERPARAMETROS_BASE, columnas=columnas,
        )

        modelo_base, r2_externo = self.modelo_base(HIPERPARAMETROS_BASE, columnas)
        cv = metricas_validacion_cruzada(
            self.train, self.particiones,
            lambda _ent, df_val: predecir_con_modelo(modelo_base, df_val, columnas),
        )
        self._agregar(
            'Modelo base sin ajuste', GRUPO_REFERENCIA, cv,
            predecir_con_modelo(modelo_base, self.test, columnas),
            predecir_recursivo(modelo_base, self.historial, self.fechas_test, columnas, False),
            hiperparametros_base=HIPERPARAMETROS_BASE, columnas=columnas, r2_externo_base=r2_externo,
        )

        return self.evaluar_ajustado(
            'Configuración actual (vigente)', GRUPO_REFERENCIA,
            HIPERPARAMETROS_BASE, HIPERPARAMETROS_AJUSTE, columnas,
        )

    def buscar_ajuste(self, hp_base, columnas, nombre, grupo):
        """Experimento 1 (y la combinada): búsqueda con validación cruzada
        temporal y evaluación en test de la configuración elegida."""
        modelo_base, _ = self.modelo_base(hp_base, columnas)
        candidatos = buscar_hiperparametros_ajuste(
            self.train, self.particiones, modelo_base.get_booster(), columnas,
            self.n_iteraciones, self.semilla,
        )
        mejor = candidatos[0]
        fila = self.evaluar_ajustado(
            nombre, grupo, hp_base, mejor['hiperparametros'], columnas, cv=mejor['cv'],
        )
        return fila, candidatos

    def evaluar_variantes_base(self):
        """Experimento 2: otras combinaciones de árboles y learning rate del
        preentrenamiento, cada una ajustada con HIPERPARAMETROS_AJUSTE."""
        filas = []
        for n_arboles, learning_rate in itertools.product(ARBOLES_BASE, LEARNING_RATES_BASE):
            hp_base = {**HIPERPARAMETROS_BASE, 'n_estimators': n_arboles, 'learning_rate': learning_rate}
            if hp_base == HIPERPARAMETROS_BASE:
                continue
            filas.append(self.evaluar_ajustado(
                f'Base {n_arboles} árboles, learning rate {learning_rate}', GRUPO_BASE,
                hp_base, HIPERPARAMETROS_AJUSTE, COLUMNAS_FEATURES,
            ))
        return filas

    def evaluar_variantes_variables(self, modelo_actual):
        """Experimento 3: cada variante de variables por separado, con el
        modelo base reentrenado con esas mismas variables y el ajuste con
        HIPERPARAMETROS_AJUSTE. Las variables a quitar se eligen por su
        importancia en el modelo ajustado actual (entrenado solo con los días
        de entrenamiento)."""
        self.importancias = importancia_variables(modelo_actual, COLUMNAS_FEATURES)
        self.variables_quitadas = [v for v, _ in self.importancias[-self.n_quitar:]] if self.n_quitar else []

        variantes = [
            ('+ rezago_1 y media_movil_3', COLUMNAS_FEATURES + ['rezago_1', 'media_movil_3']),
            ('+ inicio y fin de mes', COLUMNAS_FEATURES + ['es_inicio_mes', 'es_fin_mes']),
        ]
        if self.variables_quitadas:
            variantes.append((
                f'− {", ".join(self.variables_quitadas)} (menor importancia)',
                [c for c in COLUMNAS_FEATURES if c not in self.variables_quitadas],
            ))
        return [
            self.evaluar_ajustado(nombre, GRUPO_VARIABLES, HIPERPARAMETROS_BASE, HIPERPARAMETROS_AJUSTE, columnas)
            for nombre, columnas in variantes
        ]

    def estimar_ruido_semilla(self, n_semillas):
        """Repite la configuración actual (base reentrenada + ajuste) con
        semillas 0..n-1: la dispersión de su R² mide cuánto cambia el
        resultado solo por el azar del submuestreo, sin cambiar nada de la
        configuración. Diferencias entre filas de la tabla menores que esa
        dispersión no se pueden atribuir a la configuración. No agrega filas
        a la tabla. Devuelve una lista de {'semilla', 'r2_cv', 'r2',
        'r2_recursivo'}."""
        corridas = []
        for semilla in range(n_semillas):
            hp_base = _con_semilla(HIPERPARAMETROS_BASE, semilla)
            hp_ajuste = _con_semilla(HIPERPARAMETROS_AJUSTE, semilla)
            modelo_base = entrenar_modelo(self.train_externo, hp_base)
            booster = modelo_base.get_booster()
            modelo = ajustar(self.train, booster, hp_ajuste, COLUMNAS_FEATURES)
            corridas.append({
                'semilla': semilla,
                'r2_cv': cv_ajuste(self.train, self.particiones, booster, hp_ajuste, COLUMNAS_FEATURES)['r2'],
                'r2': calcular_metricas(
                    self.test['cantidad'], predecir_con_modelo(modelo, self.test),
                )['r2'],
                'r2_recursivo': r2_recursivo(
                    predecir_recursivo(modelo, self.historial, self.fechas_test, COLUMNAS_FEATURES, False),
                    self.test,
                ),
            })
        self.ruido_semilla = corridas
        return corridas

    def ejecutar(self):
        """Corre los tres experimentos y la combinada. Devuelve las filas
        ordenadas por R² en test (de mayor a menor)."""
        self.registrar('Referencias...')
        actual = self.evaluar_referencias()

        self.registrar(f'Experimento 1: búsqueda aleatoria del ajuste ({self.n_iteraciones} candidatos + actual)...')
        optimizada, self.candidatos_ajuste = self.buscar_ajuste(
            HIPERPARAMETROS_BASE, COLUMNAS_FEATURES, 'Ajuste optimizado por CV', GRUPO_AJUSTE,
        )

        self.registrar('Experimento 2: variantes de la fase base...')
        variantes_base = self.evaluar_variantes_base()

        self.registrar('Experimento 3: variantes de variables...')
        variantes_variables = self.evaluar_variantes_variables(actual['modelo'])

        # Combinada: la mejor base y el mejor conjunto de variables según la
        # validación cruzada (nunca según el test), con su propia búsqueda
        # del ajuste. Si ambas son las actuales, es igual al experimento 1.
        mejor_base = max([actual] + variantes_base, key=lambda f: f['cv']['r2'])
        mejores_variables = max([actual] + variantes_variables, key=lambda f: f['cv']['r2'])
        self.combinada = None
        if mejor_base is not actual or mejores_variables is not actual:
            self.registrar('Combinada: mejor base y mejores variables por CV, con búsqueda del ajuste...')
            nombre_base = 'base actual' if mejor_base is actual else mejor_base['nombre']
            nombre_variables = (
                'variables actuales' if mejores_variables is actual else mejores_variables['nombre']
            )
            self.combinada, _ = self.buscar_ajuste(
                mejor_base['hiperparametros_base'], mejores_variables['columnas'],
                f'Combinada por CV ({nombre_base}; {nombre_variables})',
                GRUPO_COMBINADA,
            )
        self.optimizada = optimizada
        self.actual = actual
        return self.filas_ordenadas()

    def filas_ordenadas(self):
        return sorted(self.filas, key=lambda f: -f['test']['r2'])
