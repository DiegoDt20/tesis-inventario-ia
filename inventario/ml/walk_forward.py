"""Validación de origen móvil (walk-forward) del motor de predicción.

Con un histórico corto, una sola división train/test deja un test de pocas
filas cuyo resultado depende de qué semana cayó en él. La evaluación sobre
un origen de pronóstico móvil (Tashman, 2000; Hyndman & Athanasopoulos,
"Forecasting: Principles and Practice", evaluation on a rolling forecasting
origin) repite el procedimiento para cada origen t:

  1. ajusta el modelo solo con los días 1..t (el modelo base preentrenado
     con Kaggle no se vuelve a entrenar; solo se rehace el ajuste fino);
  2. pronostica los días t+1..t+H, encadenando: cada día pronosticado
     alimenta las variables del siguiente, como hace predecir_demanda (sin
     ver la demanda real de los días intermedios);
  3. compara contra la demanda real de esos días;
  4. avanza t un día.

Se parte por fecha, no por fila: todas las categorías de un mismo día caen
siempre del mismo lado. El horizonte H es 1 (diario) o el lead time (la
ventana que usa el punto de reorden, ROP = d·L + SS): en ese caso se
compara la suma pronosticada de los H días contra la suma real.

Las tres líneas base (media por categoría, media móvil de 7 días, último
valor) se evalúan con el mismo procedimiento y el mismo horizonte.
"""
import math
import statistics
from collections import Counter

import numpy as np
import pandas as pd

from inventario.decisiones.motor import lead_time_producto
from inventario.models import Producto

from .entrenamiento import HIPERPARAMETROS_AJUSTE, MIN_DIAS_ENTRENAMIENTO, entrenar_modelo
from .evaluacion import LINEAS_BASE, calcular_wape, comparar_metodos
from .features import COLUMNAS_FEATURES, construir_features
from .optimizacion import predecir_recursivo

HORIZONTE_DIARIO = 1

# Con menos orígenes que esto la métrica sigue siendo frágil (la dispersión
# entre orígenes se estima con muy pocos puntos) y el comando lo advierte.
MIN_ORIGENES_CONFIABLE = 5

METODOS = ['modelo', *LINEAS_BASE]


class HistoricoInsuficiente(Exception):
    """No hay ni un origen evaluable con el histórico disponible."""


def horizonte_lead_time(categorias):
    """Horizonte de la ventana de lead time, en días: la mediana, redondeada
    hacia arriba, del lead time que usa el motor de decisiones para el stock
    de seguridad (decisiones.motor.lead_time_producto: promedio real de
    compras recibidas o, sin compras suficientes, Producto.lead_time_dias)
    de los productos activos de las categorías evaluadas. Hacia arriba para
    que la ventana cubra el lead time completo.

    Devuelve (horizonte, {lead_time: cantidad de productos}). Lanza
    ValueError si no hay productos activos en esas categorías."""
    lead_times = [
        lead_time_producto(producto)[0]
        for producto in Producto.objects.filter(activo=True, categoria__in=categorias)
    ]
    if not lead_times:
        raise ValueError('No hay productos activos en las categorías evaluadas para leer el lead time.')
    return math.ceil(statistics.median(lead_times)), dict(sorted(Counter(lead_times).items()))


def densificar_periodo(df):
    """Cada serie a diario desde su primer día hasta el ÚLTIMO día del
    periodo (de cualquier serie), con cero los días sin pedidos. Así el
    historial hasta un origen t incluye los días sin venta previos a t."""
    fecha_fin = pd.to_datetime(df['fecha']).max()
    piezas = []
    for serie_id, grupo in df.groupby('serie_id', sort=True):
        diaria = grupo.groupby(pd.to_datetime(grupo['fecha']))['cantidad'].sum()
        rango = pd.date_range(diaria.index.min(), fecha_fin, freq='D')
        piezas.append(pd.DataFrame({
            'fecha': rango,
            'serie_id': serie_id,
            'cantidad': diaria.reindex(rango, fill_value=0.0).to_numpy(dtype=float),
        }))
    return pd.concat(piezas, ignore_index=True)


def calcular_origenes(fechas, horizonte, min_dias_ajuste=MIN_DIAS_ENTRENAMIENTO):
    """Orígenes de pronóstico sobre los días del periodo (en orden y sin
    huecos): desde el día que completa `min_dias_ajuste` días de ajuste hasta
    el último t con t + horizonte <= último día. Lista vacía si no alcanza
    ninguno (cantidad = días - horizonte - min_dias_ajuste + 1)."""
    fechas = list(fechas)
    return fechas[min_dias_ajuste - 1:len(fechas) - horizonte]


# --- Pronosticadores de línea base ------------------------------------------
#
# Reciben el historial (fecha, serie_id, cantidad) SOLO hasta el origen y las
# fechas a pronosticar; devuelven fecha, serie_id, prediccion.

def _encadenar(historial, fechas_objetivo, paso):
    """Pronóstico encadenado por serie: paso(valores) da el día siguiente a
    partir de los valores anteriores, y cada pronóstico se agrega a esos
    valores para el día que sigue."""
    filas = []
    for serie_id, grupo in historial.sort_values('fecha').groupby('serie_id', sort=True):
        valores = grupo['cantidad'].astype(float).tolist()
        for fecha in fechas_objetivo:
            prediccion = float(paso(np.asarray(valores)))
            valores.append(prediccion)
            filas.append((fecha, serie_id, prediccion))
    return pd.DataFrame(filas, columns=['fecha', 'serie_id', 'prediccion'])


def pronostico_media_categoria(historial, fechas_objetivo):
    """Media de cada serie en los días de ajuste del origen, repetida."""
    medias = historial.groupby('serie_id')['cantidad'].mean()
    return pd.DataFrame(
        [(fecha, serie_id, float(media)) for serie_id, media in medias.items() for fecha in fechas_objetivo],
        columns=['fecha', 'serie_id', 'prediccion'],
    )


def pronostico_media_movil_7(historial, fechas_objetivo):
    """Media de los 7 días anteriores, encadenada (después del primer día
    entran los propios pronósticos, nunca la demanda real del tramo)."""
    return _encadenar(historial, fechas_objetivo, lambda valores: valores[-7:].mean())


def pronostico_ultimo_valor(historial, fechas_objetivo):
    """Demanda del día del origen, repetida (persistencia encadenada)."""
    return _encadenar(historial, fechas_objetivo, lambda valores: valores[-1])


PRONOSTICADORES_BASE = {
    'media_categoria': pronostico_media_categoria,
    'media_movil_7': pronostico_media_movil_7,
    'ultimo_valor': pronostico_ultimo_valor,
}


# --- Procedimiento --------------------------------------------------------------

class ValidacionOrigenMovil:
    """Evalúa el ajuste fino y las líneas base sobre todos los orígenes.

    df_interno: fecha, serie_id (categoría), cantidad. booster_base: el
    modelo base preentrenado (no se modifica; XGBoost lo copia al ajustar).
    `ajustar(train_features)` permite reemplazar el ajuste (tests, o la
    división única con el modelo vigente ya entrenado); por defecto continúa
    el entrenamiento de booster_base con `hiperparametros_ajuste`."""

    def __init__(self, df_interno, booster_base=None, hiperparametros_ajuste=None,
                 min_dias_ajuste=MIN_DIAS_ENTRENAMIENTO, ajustar=None):
        self.serie = densificar_periodo(df_interno)
        self.fechas = list(pd.date_range(self.serie['fecha'].min(), self.serie['fecha'].max(), freq='D'))
        self.min_dias_ajuste = min_dias_ajuste
        self.hiperparametros_ajuste = hiperparametros_ajuste or HIPERPARAMETROS_AJUSTE
        self._ajustar = ajustar or (
            lambda train: entrenar_modelo(train, self.hiperparametros_ajuste, xgb_model=booster_base)
        )
        self._modelos = {}

    def origenes(self, horizonte):
        return calcular_origenes(self.fechas, horizonte, self.min_dias_ajuste)

    def historial_hasta(self, origen):
        """Demanda real de los días <= origen: lo único que ve ese origen."""
        return self.serie[self.serie['fecha'] <= origen].reset_index(drop=True)

    def modelo(self, origen):
        """Modelo ajustado solo con los días 1..origen (se reutiliza entre
        horizontes)."""
        if origen not in self._modelos:
            self._modelos[origen] = self._ajustar(construir_features(self.historial_hasta(origen)))
        return self._modelos[origen]

    def pronosticar(self, origen, horizonte):
        """Pronóstico encadenado de los días origen+1..origen+horizonte, del
        modelo y de cada línea base, junto a la demanda real. Una fila por
        día y categoría: origen, fecha, categoria, real y una columna por
        método."""
        historial = self.historial_hasta(origen)
        fechas_objetivo = list(pd.date_range(origen + pd.Timedelta(days=1), periods=horizonte, freq='D'))

        resultado = self.serie[self.serie['fecha'].isin(fechas_objetivo)].rename(
            columns={'serie_id': 'categoria', 'cantidad': 'real'},
        )
        pronosticos = {
            'modelo': predecir_recursivo(self.modelo(origen), historial, fechas_objetivo, COLUMNAS_FEATURES, False),
            **{clave: funcion(historial, fechas_objetivo) for clave, funcion in PRONOSTICADORES_BASE.items()},
        }
        for clave, pronostico in pronosticos.items():
            resultado = resultado.merge(
                pronostico.rename(columns={'serie_id': 'categoria', 'prediccion': clave}),
                on=['fecha', 'categoria'], how='left', validate='one_to_one',
            )
        resultado.insert(0, 'origen', origen)
        resultado['categoria'] = resultado['categoria'].astype(str)
        return resultado[['origen', 'fecha', 'categoria', 'real', *METODOS]]

    def evaluar(self, horizonte):
        """Pronostica desde todos los orígenes con ese horizonte. Devuelve
        (ventanas, diario): diario es una fila por origen/día/categoría y
        ventanas la suma por origen/categoría (con horizonte 1 son lo mismo).
        Lanza HistoricoInsuficiente si no hay ningún origen evaluable."""
        origenes = self.origenes(horizonte)
        if not origenes:
            raise HistoricoInsuficiente(
                f'El histórico cubre {len(self.fechas)} día(s): con al menos {self.min_dias_ajuste} días de '
                f'ajuste y un horizonte de {horizonte} día(s) se necesitan '
                f'{self.min_dias_ajuste + horizonte} días para evaluar un solo origen.'
            )
        diario = pd.concat([self.pronosticar(origen, horizonte) for origen in origenes], ignore_index=True)
        return agregar_por_ventana(diario), diario


def agregar_por_ventana(diario):
    """Suma real y pronósticos de cada origen y categoría sobre los días de
    su horizonte: una fila por ventana (origen, categoria, fecha_inicio,
    fecha_fin, dias, real y una columna por método)."""
    agregado = diario.groupby(['origen', 'categoria'], sort=True).agg(
        fecha_inicio=('fecha', 'min'), fecha_fin=('fecha', 'max'), dias=('fecha', 'count'),
        real=('real', 'sum'), **{clave: (clave, 'sum') for clave in METODOS if clave in diario},
    )
    return agregado.reset_index()


def dispersion_entre_origenes(ventanas, columna):
    """Dispersión del WAPE ENTRE ORÍGENES (no entre filas): se calcula el
    WAPE total de cada origen (todas sus categorías) y luego la media y la
    desviación estándar muestral (ddof=1) de esos valores, uno por origen.
    Igual por categoría ('por_categoria'). Un origen sin demanda real (WAPE
    no definido) no entra."""
    def resumen(wapes):
        valores = np.array(list(wapes.values()), dtype=float)
        return {
            'n_origenes': len(valores),
            'media': float(valores.mean()) if len(valores) else None,
            'desviacion': float(valores.std(ddof=1)) if len(valores) >= 2 else None,
            'minimo': float(valores.min()) if len(valores) else None,
            'maximo': float(valores.max()) if len(valores) else None,
        }

    def wapes_por_origen(filas):
        wapes = {}
        for origen, grupo in filas.groupby('origen', sort=True):
            wape = calcular_wape(grupo['real'], grupo[columna])
            if wape is not None:
                wapes[pd.Timestamp(origen).strftime('%Y-%m-%d')] = wape
        return wapes

    total = wapes_por_origen(ventanas)
    return {
        **resumen(total),
        'wape_por_origen': total,
        'por_categoria': {
            str(categoria): resumen(wapes_por_origen(grupo))
            for categoria, grupo in ventanas.groupby('categoria', sort=True)
        },
    }


def resumir(ventanas, tolerancia_relativa=None):
    """Métricas de exactitud del modelo y de las líneas base sobre las
    ventanas (comparar_metodos) más la dispersión entre orígenes de cada
    método ('dispersion')."""
    resultado = comparar_metodos(ventanas, tolerancia_relativa)
    resultado['dispersion'] = {clave: dispersion_entre_origenes(ventanas, clave) for clave in METODOS}
    return resultado
