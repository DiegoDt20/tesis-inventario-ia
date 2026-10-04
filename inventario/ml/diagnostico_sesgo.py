"""Diagnóstico de un sesgo sistemático del pronóstico en una categoría
(comando diagnosticar_sesgo).

Es diagnóstico, no optimización: primero se mide qué tipo de sesgo es y se
contrastan cuatro hipótesis con una medición cada una; solo si una queda
identificada como causa dominante se evalúa la corrección que le
corresponde (y únicamente esa).

  (a) el sesgo viene del modelo base (Kaggle) y no del ajuste fino;
  (b) el piso en cero de las predicciones introduce asimetría;
  (c) las medias/desviación móviles se degradan con muchos días en cero;
  (d) la categoría mezcla productos de rotación muy distinta.

Los criterios de identificación están en las constantes de abajo y no se
ajustan según el resultado.

Convención de signo en todo el módulo: error = pronóstico - real. Positivo
es sobreestimación, negativo subestimación.
"""
import numpy as np
import pandas as pd

from inventario.models import PedidoDetalle, Producto

from .evaluacion import calcular_wape
from .features import COLUMNAS_FEATURES, construir_features

# (a) y (b): una hipótesis "explica" el sesgo si da cuenta de al menos esta
# fracción del exceso (o defecto) del modelo vigente, con el mismo signo.
FRACCION_CAUSA = 0.5

# (c): la categoría tiene "muchos más ceros" si su proporción de días sin
# demanda es al menos este múltiplo de la mayor entre las demás categorías.
MULTIPLO_CEROS = 2.0

# Niveles constantes de historial con que se traza la curva de respuesta
# del modelo (unidades diarias).
NIVELES_CURVA = [0, 2, 5, 8, 10, 15, 20, 30, 40, 60]


# --- 1. Caracterización del sesgo -----------------------------------------------

def error_con_signo(real, pronostico):
    """Error con signo, pronóstico - real (positivo = sobreestimación)."""
    return np.asarray(pronostico, dtype=float) - np.asarray(real, dtype=float)


def sesgo_relativo(real, pronostico):
    """Σ(pronóstico - real) / Σ real, o None si Σ real == 0. A diferencia
    del WAPE, los errores de signo contrario se compensan: mide el sesgo,
    no la dispersión."""
    suma_real = float(np.sum(np.asarray(real, dtype=float)))
    if suma_real == 0:
        return None
    return float(error_con_signo(real, pronostico).sum() / suma_real)


def resumen_sesgo(filas, columna='modelo'):
    """Por categoría: filas, demanda real y pronosticada (sumas), error
    medio con signo, sesgo relativo y cuántas filas tienen error positivo,
    negativo o cero. `filas` puede ser diario (una fila por día) o ventanas
    (una fila por origen: entonces 'positivas' es la cantidad de orígenes
    con sobreestimación)."""
    resumen = {}
    for categoria, grupo in filas.groupby('categoria', sort=True):
        error = error_con_signo(grupo['real'], grupo[columna])
        resumen[str(categoria)] = {
            'filas': int(len(grupo)),
            'real': float(grupo['real'].sum()),
            'pronostico': float(grupo[columna].sum()),
            'error_medio': float(error.mean()) if len(error) else None,
            'sesgo_relativo': sesgo_relativo(grupo['real'], grupo[columna]),
            'positivas': int((error > 0).sum()),
            'negativas': int((error < 0).sum()),
            'cero': int((error == 0).sum()),
        }
    return resumen


def error_por_posicion(diario, columna='modelo'):
    """Error medio con signo según la POSICIÓN del día dentro de su ventana
    (1 = día siguiente al origen, ..., H), no según la fecha: una misma
    fecha es el día 1 de un origen y el día 3 de otro. Devuelve
    {categoria: {posicion: error medio}}. Si el error crece con la
    posición, el sesgo se acumula al encadenar."""
    diario = diario.assign(
        posicion=(pd.to_datetime(diario['fecha']) - pd.to_datetime(diario['origen'])).dt.days,
        error=error_con_signo(diario['real'], diario[columna]),
    )
    tabla = diario.groupby(['categoria', 'posicion'])['error'].mean()
    return {
        str(categoria): {int(posicion): float(valor) for posicion, valor in grupo.droplevel(0).items()}
        for categoria, grupo in tabla.groupby(level=0, sort=True)
    }


# --- 2(a). Modelo base frente al ajustado -----------------------------------------

def curva_respuesta(modelo, fechas, niveles=NIVELES_CURVA, desde=0):
    """Pronóstico medio (a un paso) del modelo cuando el historial es
    constante a cada nivel: todas las variables de rezago y media móvil
    valen ese nivel. Usa las fechas del periodo evaluado (mismas variables
    de calendario) y promedia desde la posición `desde`. Si para niveles
    bajos la salida no baja del mismo valor, el modelo tiene un piso: no
    aprendió a pronosticar por debajo de él. Devuelve {nivel: pronóstico}."""
    fechas = list(fechas)
    curva = {}
    for nivel in niveles:
        historial = pd.DataFrame({'fecha': fechas, 'serie_id': 'nivel', 'cantidad': float(nivel)})
        features = construir_features(historial).iloc[desde:]
        curva[nivel] = float(np.mean(modelo.predict(features[COLUMNAS_FEATURES])))
    return curva


def niveles_serie_externa(df_externo, nivel):
    """Dónde cae `nivel` (unidades diarias) dentro del dataset externo:
    media mínima, mediana y máxima por serie, y fracción de filas (días) con
    demanda menor o igual a `nivel`."""
    medias = df_externo.groupby('serie_id')['cantidad'].mean()
    return {
        'media_serie_minima': float(medias.min()),
        'media_serie_mediana': float(medias.median()),
        'media_serie_maxima': float(medias.max()),
        'fraccion_dias_hasta_nivel': float((df_externo['cantidad'] <= nivel).mean()),
    }


# --- 2(b). Piso en cero ---------------------------------------------------------

def conteo_recortes(diario, columna='modelo', cruda='modelo_crudo'):
    """Por categoría: cuántas predicciones crudas fueron negativas (las que
    el piso llevó a 0), cuánto suma ese recorte (lo que el piso agregó al
    pronóstico) y el sesgo relativo con y sin recorte. "Sin recorte" usa la
    salida cruda de las mismas predicciones (el encadenamiento ya usó la de
    piso): mide cuánto aporta el piso en sí, solo para diagnóstico."""
    resultado = {}
    for categoria, grupo in diario.groupby('categoria', sort=True):
        crudas = grupo[cruda].to_numpy(dtype=float)
        negativas = crudas < 0
        resultado[str(categoria)] = {
            'predicciones': int(len(crudas)),
            'recortadas': int(negativas.sum()),
            'suma_recortada': float(np.abs(crudas[negativas]).sum()),
            'sesgo_con_recorte': sesgo_relativo(grupo['real'], grupo[columna]),
            'sesgo_sin_recorte': sesgo_relativo(grupo['real'], crudas),
        }
    return resultado


# --- 2(c). Días en cero -----------------------------------------------------------

def proporcion_dias_cero(serie):
    """Fracción de días con demanda cero por serie (fecha, serie_id,
    cantidad, ya densificada a diario)."""
    return {
        str(serie_id): float((grupo['cantidad'] == 0).mean())
        for serie_id, grupo in serie.groupby('serie_id', sort=True)
    }


# --- 2(d). Productos de la categoría -----------------------------------------------

def distribucion_productos(categoria, origen):
    """Demanda (cantidad_solicitada) de cada producto activo de la categoría
    en los pedidos del origen: unidades, días con pedido, participación.
    Incluye los productos sin pedidos. Ordenada de mayor a menor."""
    productos = Producto.objects.filter(categoria=categoria, activo=True).values('id', 'codigo', 'nombre')
    filas = {p['id']: {'codigo': p['codigo'], 'nombre': p['nombre'], 'unidades': 0.0, 'dias': set()}
             for p in productos}
    pedidos = PedidoDetalle.objects.filter(
        producto__categoria=categoria, producto__activo=True, pedido__origen=origen,
    ).values('producto_id', 'cantidad_solicitada', 'pedido__fecha_solicitud')
    for linea in pedidos:
        fila = filas[linea['producto_id']]
        fila['unidades'] += float(linea['cantidad_solicitada'])
        fila['dias'].add(pd.Timestamp(linea['pedido__fecha_solicitud']).date())
    total = sum(f['unidades'] for f in filas.values())
    resultado = [
        {
            'codigo': f['codigo'], 'nombre': f['nombre'], 'unidades': f['unidades'],
            'dias_con_pedido': len(f['dias']),
            'participacion': f['unidades'] / total if total else 0.0,
        }
        for f in filas.values()
    ]
    return sorted(resultado, key=lambda f: (-f['unidades'], f['codigo']))


def resumen_productos(distribucion):
    """Concentración de la demanda: productos con y sin pedidos,
    participación de los 3 mayores y días con pedido (máximo y mediana entre
    los que tienen pedidos)."""
    con_pedidos = [f for f in distribucion if f['unidades'] > 0]
    dias = [f['dias_con_pedido'] for f in con_pedidos]
    return {
        'productos': len(distribucion),
        'con_pedidos': len(con_pedidos),
        'sin_pedidos': len(distribucion) - len(con_pedidos),
        'participacion_top3': float(sum(f['participacion'] for f in distribucion[:3])),
        'dias_con_pedido_maximo': max(dias) if dias else 0,
        'dias_con_pedido_mediana': float(np.median(dias)) if dias else 0.0,
    }


# --- Corrección: comparación pareada ----------------------------------------------

def comparar_por_origen(ventanas_antes, ventanas_despues, columna='modelo'):
    """Comparación pareada del WAPE antes y después de una corrección, sobre
    los mismos orígenes: en cuántos baja y en cuántos sube, en total (todas
    las categorías del origen) y por categoría. Un origen sin demanda real
    (WAPE no definido) no cuenta."""
    def wapes(ventanas):
        return {
            origen: calcular_wape(grupo['real'], grupo[columna])
            for origen, grupo in ventanas.groupby('origen', sort=True)
        }

    def contar(antes, despues):
        comunes = [o for o in antes if o in despues and antes[o] is not None and despues[o] is not None]
        return {
            'origenes': len(comunes),
            'mejoran': sum(despues[o] < antes[o] for o in comunes),
            'empeoran': sum(despues[o] > antes[o] for o in comunes),
        }

    return {
        'total': contar(wapes(ventanas_antes), wapes(ventanas_despues)),
        'por_categoria': {
            str(categoria): contar(
                wapes(grupo), wapes(ventanas_despues[ventanas_despues['categoria'] == categoria]),
            )
            for categoria, grupo in ventanas_antes.groupby('categoria', sort=True)
        },
    }


# --- Identificación -----------------------------------------------------------------

def _fraccion(parte, total):
    """Fracción del sesgo total que explica `parte`, con signo (negativa si
    va en dirección contraria). None si no hay sesgo."""
    if total is None or parte is None or total == 0:
        return None
    return parte / total


def identificar_causa(categoria, sesgo_vigente, sesgo_base, recortes, ceros, sesgo_media_movil):
    """Aplica los criterios fijos a las mediciones de la categoría.

    - sesgo_vigente / sesgo_base: sesgo relativo del modelo vigente y del
      base sin ajustar, sobre los mismos orígenes.
    - recortes: conteo_recortes(...)[categoria] del vigente.
    - ceros: proporcion_dias_cero(...) de todas las categorías.
    - sesgo_media_movil: sesgo relativo de la línea base de media móvil de 7
      días (construida con las mismas medias móviles que usa el modelo).

    (a) identificada si el base solo ya da cuenta de >= FRACCION_CAUSA del
    sesgo del vigente, con el mismo signo. (b) si lo que suma el recorte da
    cuenta de >= FRACCION_CAUSA del exceso (solo puede subir el pronóstico:
    no explica una subestimación). (c) si la categoría tiene >=
    MULTIPLO_CEROS veces la proporción de ceros de las demás Y la media
    móvil de 7 días tiene sesgo del mismo signo (si las propias medias
    móviles no llevan el sesgo, los ceros no son lo que lo produce). (d) no
    es contrastable como causa (el modelo nunca ve productos): se reporta
    como descripción. La dominante es la identificada que explica la mayor
    fracción; None si ninguna."""
    exceso_recorte = None
    if recortes['sesgo_con_recorte'] is not None and recortes['sesgo_sin_recorte'] is not None:
        exceso_recorte = recortes['sesgo_con_recorte'] - recortes['sesgo_sin_recorte']

    fraccion_a = _fraccion(sesgo_base, sesgo_vigente)
    fraccion_b = _fraccion(exceso_recorte, sesgo_vigente) if sesgo_vigente and sesgo_vigente > 0 else None
    otras = [v for c, v in ceros.items() if c != categoria]
    mayor_otras = max(otras) if otras else 0.0
    muchos_ceros = ceros.get(categoria, 0.0) >= MULTIPLO_CEROS * mayor_otras if mayor_otras else False
    mismo_signo_mm = (
        sesgo_vigente is not None and sesgo_media_movil is not None
        and np.sign(sesgo_media_movil) == np.sign(sesgo_vigente)
    )

    hipotesis = {
        'a': {
            'identificada': fraccion_a is not None and fraccion_a >= FRACCION_CAUSA,
            'fraccion_explicada': fraccion_a,
        },
        'b': {
            'identificada': fraccion_b is not None and fraccion_b >= FRACCION_CAUSA,
            'fraccion_explicada': fraccion_b,
        },
        'c': {
            'identificada': bool(muchos_ceros and mismo_signo_mm),
            'muchos_ceros': bool(muchos_ceros),
            'media_movil_mismo_signo': bool(mismo_signo_mm),
        },
        'd': {'identificada': False, 'contrastable': False},
    }
    candidatas = {
        clave: h.get('fraccion_explicada') or 0.0
        for clave, h in hipotesis.items() if h['identificada']
    }
    dominante = max(candidatas, key=candidatas.get) if candidatas else None
    return {'hipotesis': hipotesis, 'dominante': dominante}
