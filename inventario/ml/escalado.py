"""Escalado de nivel de las series internas antes del ajuste fino.

Es la corrección que corresponde a la hipótesis (a) del diagnóstico de
sesgo (comando diagnosticar_sesgo): el modelo base se preentrenó con series
de Kaggle cuya demanda media va de ~13 a ~113 unidades diarias, así que no
aprendió a pronosticar por debajo de ese rango y, en una serie interna de
nivel más bajo (accesorio), impone un piso que la sobreestima.

La idea es que la transferencia aporte la forma temporal y no el nivel:
una serie interna cuyo nivel medio (en los días de ajuste) queda por
debajo de la serie externa de menor nivel se multiplica por un factor que
la lleva al nivel típico del dataset externo (la mediana de las medias por
serie), se ajusta y pronostica en esa escala, y el pronóstico se divide por
el mismo factor. Las series que ya están dentro del rango externo no se
tocan (factor 1).

Ambos niveles se fijaron antes de evaluar la corrección y salen del propio
dataset externo; no se ajustan según el resultado. Esto CAMBIA el
procedimiento de entrenamiento: si se adopta en producción (entrenar_ajustado
y predecir_demanda), hay que reflejarlo en la metodología.
"""


REGLA = (
    'Una serie cuya media diaria en los días de ajuste es menor que la media de la serie de menor '
    'nivel del dataset externo se multiplica por (mediana de las medias por serie del dataset externo '
    '/ su media) antes del ajuste fino; el pronóstico se divide por el mismo factor. Las demás '
    'series, factor 1.'
)


def niveles_externos(df_externo):
    """Nivel medio diario de cada serie del dataset externo (fecha,
    serie_id, cantidad). Devuelve {'minimo': media de la serie de menor
    nivel, 'referencia': mediana de las medias por serie}."""
    medias = df_externo.groupby('serie_id')['cantidad'].mean()
    return {'minimo': float(medias.min()), 'referencia': float(medias.median())}


def factores_escala(historial, nivel_minimo, nivel_referencia):
    """Factor de cada serie de `historial` (solo los días de ajuste: lo
    único que ve ese origen). Una serie con media diaria positiva y menor
    que nivel_minimo recibe nivel_referencia / media; las demás, 1.0 (una
    serie sin demanda no tiene nivel que escalar)."""
    medias = historial.groupby('serie_id')['cantidad'].mean()
    return {
        serie_id: (nivel_referencia / media if 0 < media < nivel_minimo else 1.0)
        for serie_id, media in medias.items()
    }


def escalar(df, factores, columnas=('cantidad',)):
    """Copia de df con `columnas` multiplicadas por el factor de su serie
    (serie_id; las series sin factor quedan igual). Para volver a la escala
    original se pasan los factores inversos. Las claves se comparan como
    texto: los factores guardados en JSON tienen claves de texto aunque la
    serie sea el id (entero) de un producto."""
    df = df.copy()
    por_texto = {str(serie_id): factor for serie_id, factor in factores.items()}
    multiplicador = df['serie_id'].astype(str).map(por_texto).fillna(1.0).astype(float)
    for columna in columnas:
        df[columna] = df[columna].astype(float) * multiplicador
    return df


class EscaladoNivel:
    """Calcula los factores de un origen a partir de su historial (para
    ValidacionOrigenMovil(escalado=...))."""

    def __init__(self, nivel_minimo, nivel_referencia):
        self.nivel_minimo = nivel_minimo
        self.nivel_referencia = nivel_referencia

    def __call__(self, historial):
        return factores_escala(historial, self.nivel_minimo, self.nivel_referencia)


def invertir(factores):
    """Factores inversos (para devolver un pronóstico a su escala original)."""
    return {serie_id: 1.0 / factor for serie_id, factor in factores.items()}


def factor_guardado(configuracion, serie_id):
    """Factor de una serie en la configuración de escalado guardada en
    ModeloEntrenado.escalado (las claves del JSON son texto). 1.0 si el
    modelo no escala o la serie no tiene factor."""
    if not configuracion:
        return 1.0
    return float(configuracion['factores'].get(str(serie_id), 1.0))


def configuracion_guardada(niveles, factores):
    """Lo que se guarda en ModeloEntrenado.escalado: la regla, los niveles
    del dataset externo y los factores calculados con los días de
    entrenamiento del modelo (los que usa predecir_demanda)."""
    return {
        'regla': REGLA,
        'nivel_minimo': niveles['minimo'],
        'nivel_referencia': niveles['referencia'],
        'factores': {str(serie_id): float(factor) for serie_id, factor in factores.items()},
    }
