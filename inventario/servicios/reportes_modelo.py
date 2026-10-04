"""Lectura de los reportes ya guardados de la evaluación del pronóstico
(evaluar_predicciones, validación de origen móvil) y del estudio de
ablación (evaluar_ablacion), para la pantalla /modelo/.

No recalcula nada: toma el reporte JSON más reciente de cada directorio y lo
reordena para la plantilla. Si falta uno (o no se puede leer), devuelve
None para esa parte y la pantalla muestra lo que sí existe.
"""
import json
import logging
import re
from pathlib import Path

from django.conf import settings
from django.utils.dateparse import parse_date, parse_datetime
from django.utils.formats import get_format

from inventario.models import Categoria

logger = logging.getLogger(__name__)

DIR_EVALUACION = Path(settings.BASE_DIR) / 'artefactos' / 'evaluacion'
DIR_ABLACION = Path(settings.BASE_DIR) / 'artefactos' / 'ablacion'

# Solo la validación de origen móvil: la división única es otra evaluación.
PATRON_EVALUACION = 'evaluacion_walkforward_*.json'
PATRON_ABLACION = 'ablacion_*.json'

# Horizonte principal (el de la decisión de compra) primero.
HORIZONTES = [
    ('ventana', 'Ventana de lead time', True),
    ('diario', 'Diario', False),
]

NOMBRES_LINEAS_BASE = {
    'media_categoria': 'Media por categoría',
    'media_movil_7': 'Media móvil 7 días',
    'ultimo_valor': 'Último valor',
}

# Nombres cortos de las configuraciones de la ablación para las comparaciones.
ETIQUETAS_CORTAS = {'A': 'A', 'A_escalado': 'A con escalado', 'B': 'B', 'C': 'C'}

# Categoría cuyo sesgo se muestra en la ablación (ver el diagnóstico de
# sesgo de accesorio en el README).
CATEGORIA_SESGO = 'accesorio'


def ultimo_reporte(directorio, patron):
    """(ruta, contenido) del reporte más reciente (por nombre: llevan la
    fecha y hora en el nombre), o (None, None) si no hay o no se puede
    leer."""
    rutas = sorted(Path(directorio).glob(patron)) if Path(directorio).is_dir() else []
    if not rutas:
        return None, None
    ruta = rutas[-1]
    try:
        return ruta, json.loads(ruta.read_text())
    except (OSError, ValueError):
        logger.exception('No se pudo leer el reporte %s', ruta)
        return None, None


def _fecha(reporte):
    return parse_datetime(reporte['fecha']) if reporte.get('fecha') else None


def _horizonte_evaluacion(datos, clave, titulo, principal):
    resultados = datos['resultados']
    dispersion = datos.get('dispersion_entre_origenes') or {}
    disp_modelo = dispersion.get('modelo', {})
    por_origen = datos.get('comparacion_por_origen') or {}
    modelo = resultados['modelo']
    supera = datos.get('modelo_supera_por_wape') or {}

    filas = [
        {
            'categoria': categoria, 'es_total': False,
            'exactitud': m['exactitud'], 'wape': m['wape'], 'mae': m['mae'], 'rmse': m['rmse'],
            'acierto': m['acierto_tolerancia'], 'r2': m['r2'], 'real': m['demanda_real'],
            'pronosticado': m['demanda_pronosticada'],
            'desviacion': disp_modelo.get('por_categoria', {}).get(categoria, {}).get('desviacion'),
        }
        for categoria, m in modelo['categorias'].items()
    ]
    total = modelo['total']
    filas.append({
        'categoria': 'Total', 'es_total': True,
        'exactitud': total['exactitud'], 'wape': total['wape'], 'mae': total['mae'], 'rmse': total['rmse'],
        'acierto': total['acierto_tolerancia'], 'r2': total['r2_global'], 'real': total['demanda_real'],
        'pronosticado': total['demanda_pronosticada'], 'desviacion': disp_modelo.get('desviacion'),
    })

    comparacion = [{
        'nombre': 'Modelo en producción', 'es_modelo': True,
        'exactitud': total['exactitud'], 'wape': total['wape'],
        'desviacion': disp_modelo.get('desviacion'), 'por_origen': None,
    }]
    for base, nombre in NOMBRES_LINEAS_BASE.items():
        if base not in resultados:
            continue
        comparacion.append({
            'nombre': nombre, 'es_modelo': False,
            'exactitud': resultados[base]['total']['exactitud'], 'wape': resultados[base]['total']['wape'],
            'desviacion': dispersion.get(base, {}).get('desviacion'),
            # Reportes anteriores a la prueba de signos no la traen.
            'por_origen': por_origen.get(base),
        })

    return {
        'clave': clave, 'titulo': titulo, 'principal': principal,
        'horizonte_dias': datos['horizonte_dias'], 'n_origenes': datos.get('n_origenes'),
        'origen_inicio': parse_date((datos.get('origenes') or [''])[0] or ''),
        'origen_fin': parse_date((datos.get('origenes') or [''])[-1] or ''),
        'tolerancia': modelo.get('tolerancia_relativa'),
        'filas': filas, 'comparacion': comparacion,
        'supera_todas': bool(supera) and all(supera.values()),
        # Real y pronóstico de cada ventana evaluada, tal como los guardó
        # evaluar_predicciones (para el gráfico del resumen).
        'ventanas': datos.get('ventanas') or [],
        'mejor_linea_base': NOMBRES_LINEAS_BASE.get(datos.get('mejor_linea_base')),
    }


def evaluacion_vigente(modelo_produccion=None):
    """Evaluación del pronóstico del reporte walk-forward más reciente, o
    None si no hay. 'coincide' es False si el reporte corresponde a otro
    modelo que el que está en producción."""
    ruta, reporte = ultimo_reporte(DIR_EVALUACION, PATRON_EVALUACION)
    if reporte is None:
        return None
    try:
        horizontes = [
            _horizonte_evaluacion(reporte['horizontes'][clave], clave, titulo, principal)
            for clave, titulo, principal in HORIZONTES if clave in reporte['horizontes']
        ]
    except (KeyError, TypeError):
        logger.exception('El reporte %s no tiene la estructura esperada', ruta)
        return None
    modelo = reporte.get('modelo_ajustado')
    return {
        'archivo': ruta.name, 'fecha': _fecha(reporte), 'origen': reporte.get('origen'),
        'modelo': modelo, 'modelo_base': reporte.get('modelo_base'),
        'coincide': modelo_produccion is None or modelo == modelo_produccion.pk,
        'categorias_sin_pronostico': reporte.get('categorias_sin_pronostico') or {},
        'horizontes': horizontes,
    }


def ablacion_vigente():
    """Estudio de ablación del reporte más reciente, o None si no hay: por
    configuración (y línea base), la exactitud total y el sesgo de
    accesorio en cada horizonte, más la lectura de las tres preguntas."""
    ruta, reporte = ultimo_reporte(DIR_ABLACION, PATRON_ABLACION)
    if reporte is None:
        return None
    try:
        configuraciones = reporte['configuraciones']
        filas = []
        for clave in [*configuraciones, *NOMBRES_LINEAS_BASE]:
            fila = {
                'clave': clave, 'nombre': configuraciones.get(clave, NOMBRES_LINEAS_BASE.get(clave)),
                'es_linea_base': clave not in configuraciones, 'horizontes': {},
            }
            for horizonte, _, _ in HORIZONTES:
                metodo = reporte['horizontes'][horizonte]['resumen']['metodos'][clave]
                total = metodo['exactitud']['total']
                sesgo = metodo['sesgo'].get(CATEGORIA_SESGO, {}).get('sesgo_relativo')
                fila['horizontes'][horizonte] = {
                    'exactitud': total['exactitud'], 'wape': total['wape'], 'sesgo_categoria': sesgo,
                }
            filas.append(fila)
        comparaciones = [
            {**c, 'nombre': f'{ETIQUETAS_CORTAS.get(c["configuracion"], c["configuracion"])} vs. '
                            f'{ETIQUETAS_CORTAS.get(c["contra"], c["contra"])}'}
            for c in reporte['horizontes']['ventana']['resumen']['comparaciones']
            if c['configuracion'] in configuraciones and c['contra'] in configuraciones
        ]
    except (KeyError, TypeError):
        logger.exception('El reporte %s no tiene la estructura esperada', ruta)
        return None
    return {
        'archivo': ruta.name, 'fecha': _fecha(reporte), 'precision': reporte.get('precision'),
        'modelo_vigente': reporte.get('modelo_vigente'), 'modelo_base': reporte.get('modelo_base'),
        'categoria_sesgo': CATEGORIA_SESGO, 'filas': filas, 'comparaciones': comparaciones,
        'lectura': [_linea_lectura(linea) for linea in (reporte.get('lectura') or {}).get('texto') or []],
    }


def _linea_lectura(linea):
    """Línea de la lectura tal como la escribió evaluar_ablacion, sin su
    numeración ("1. ", la pone la lista) y con el separador decimal de la
    pantalla (el comando escribe 16.4%; la pantalla, 16,4 %)."""
    linea = re.sub(r'^\d+\.\s*', '', linea)
    return re.sub(r'(?<=\d)\.(?=\d)', get_format('DECIMAL_SEPARATOR'), linea)


# --- Resumen en castellano llano (encabezado de /modelo/) --------------------
#
# Semáforo de la exactitud por categoría, fijado el 04/10/2026 antes de
# mirar cómo quedan las categorías y que no se ajusta según el resultado:
# verde desde 80 %, ámbar desde 60 % (hasta 80 %), rojo por debajo de 60 %.
# Se compara el valor del reporte, sin redondear.
SEMAFORO_VERDE_DESDE = 0.80
SEMAFORO_AMBAR_DESDE = 0.60


def color_semaforo(exactitud):
    """'verde', 'ambar' o 'rojo' según los umbrales fijos; None si la
    exactitud no está definida."""
    if exactitud is None:
        return None
    if exactitud >= SEMAFORO_VERDE_DESDE:
        return 'verde'
    if exactitud >= SEMAFORO_AMBAR_DESDE:
        return 'ambar'
    return 'rojo'


def _frase_exactitud(exactitud, horizonte_dias):
    """"De cada 100 unidades que se venden en una semana, el pronóstico
    acierta casi 84; las otras 16 se le escapan, de más o de menos." El
    "casi"/"algo más de" dice hacia dónde se redondeó."""
    aciertos = round(exactitud * 100)
    if exactitud * 100 < aciertos:
        cuantas = f'casi {aciertos}'
    elif exactitud * 100 > aciertos:
        cuantas = f'algo más de {aciertos}'
    else:
        cuantas = str(aciertos)
    periodo = 'una semana' if horizonte_dias == 7 else f'{horizonte_dias} días'
    return (
        f'De cada 100 unidades que se venden en {periodo}, el pronóstico acierta {cuantas}; '
        f'las otras {100 - aciertos} se le escapan, de más o de menos.'
    )


def resumen_pronostico(evaluacion):
    """Resumen sin jerga para el dueño, a partir de la MISMA evaluación que
    la tabla de la ventana de lead time (el horizonte sobre el que se decide
    la compra; el diario no entra). None si no hay evaluación vigente.

    Devuelve la exactitud total, la frase que la explica, el horizonte, una
    fila por categoría con su color y las categorías en ámbar o rojo, para
    la advertencia de dónde no confiar."""
    if not evaluacion or not evaluacion['coincide']:
        return None
    ventana = next((h for h in evaluacion['horizontes'] if h['clave'] == 'ventana'), None)
    if ventana is None:
        return None
    total = next(f for f in ventana['filas'] if f['es_total'])
    if total['exactitud'] is None:
        return None
    etiquetas = dict(Categoria.choices)
    categorias = [
        {
            'clave': f['categoria'], 'nombre': etiquetas.get(f['categoria'], f['categoria'].capitalize()),
            'exactitud': f['exactitud'], 'color': color_semaforo(f['exactitud']),
            # Solo para dibujar la barra: entre 0 y 100 (la exactitud puede
            # ser negativa si el error supera a la demanda).
            'ancho_barra': f'{min(max(f["exactitud"] or 0.0, 0.0), 1.0) * 100:.1f}',
        }
        for f in ventana['filas'] if not f['es_total']
    ]
    menos_confiables = [c['nombre'].lower() for c in categorias if c['color'] in ('ambar', 'rojo')]
    advertencia = None
    if len(menos_confiables) == 1:
        advertencia = (
            f'En la categoría {menos_confiables[0]} el pronóstico es menos confiable. '
            'Conviene revisar esa sugerencia antes de comprar.'
        )
    elif menos_confiables:
        advertencia = (
            f'En las categorías {", ".join(menos_confiables[:-1])} y {menos_confiables[-1]} el pronóstico es '
            'menos confiable. Conviene revisar esas sugerencias antes de comprar.'
        )
    return {
        'exactitud': total['exactitud'],
        'grafico': series_por_categoria(ventana['ventanas'], categorias),
        'frase': _frase_exactitud(total['exactitud'], ventana['horizonte_dias']),
        'horizonte_dias': ventana['horizonte_dias'],
        'categorias': categorias,
        'advertencia': advertencia,
    }


def series_por_categoria(ventanas, categorias):
    """Para el gráfico "pronóstico contra venta real": por cada categoría
    del resumen, una serie con la fecha de inicio de cada ventana evaluada y
    su demanda real y pronosticada (sumas de la ventana), copiadas del
    reporte sin recalcular. Una ventana que no está en el reporte no se
    dibuja. Lista vacía si el reporte no trae el detalle por ventana."""
    series = []
    for categoria in categorias:
        puntos = sorted(
            (
                {'fecha': v['fecha_inicio'], 'real': v['real'], 'pronostico': v['modelo']}
                for v in ventanas
                if str(v.get('categoria')) == categoria['clave'] and v.get('fecha_inicio')
                and v.get('real') is not None and v.get('modelo') is not None
            ),
            key=lambda punto: punto['fecha'],
        )
        if puntos:
            series.append({**categoria, 'id': f'grafico-ventanas-{categoria["clave"]}', 'puntos': puntos})
    return series

