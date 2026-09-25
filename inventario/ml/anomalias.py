"""Detección de anomalías de control de existencias.

Dos detectores independientes, ambos de solo lectura (no escriben en la base
de datos: eso lo hace el comando "detectar_anomalias"):

a) Diferencias de inventario (detectar_diferencias_inventario): sobre
   ConteoDetalle, detecta productos cuya diferencia entre stock de sistema y
   stock físico se aleja de lo normal. Combina un Isolation Forest (sobre la
   diferencia absoluta y la diferencia relativa al stock de sistema) con una
   regla simple: diferencia mayor al 20% del stock de sistema. La severidad
   se asigna aparte, con umbrales configurables (ANOMALIA_UMBRAL_ALTA y
   ANOMALIA_UMBRAL_MEDIA en settings). Con muy pocos
   registros (menos de MIN_REGISTROS_ISOLATION_FOREST) no tiene sentido
   ajustar un Isolation Forest, así que se usa solo la regla simple.

b) Movimientos atípicos (detectar_movimientos_atipicos): sobre Movimiento
   (solo ingresos y salidas, los ajustes no representan una "cantidad
   vendida/comprada" comparable), detecta cantidades que se desvían
   fuertemente del comportamiento habitual de cada producto usando el
   z-score sobre su propio histórico, calculado por separado para ingresos y
   para salidas. Un producto con menos de MIN_MOVIMIENTOS_ZSCORE movimientos
   de un tipo no se evalúa: no hay histórico suficiente para saber qué es
   "habitual".
"""
from dataclasses import dataclass
from datetime import datetime

import numpy as np
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone
from sklearn.ensemble import IsolationForest

from inventario.models import Anomalia, ConteoDetalle, Movimiento

# Regla simple del detector de diferencias de inventario: 20% del stock de
# sistema, tal como pide el objetivo específico 1.
UMBRAL_DIFERENCIA_RELATIVA = 0.20

# Con menos registros que esto, un Isolation Forest no tiene suficientes
# datos para aprender qué es "normal"; se usa solo la regla del 20%.
MIN_REGISTROS_ISOLATION_FOREST = 5

# Un producto necesita al menos esta cantidad de movimientos (de un mismo
# tipo: ingreso o salida) para poder calcularle un z-score con sentido.
MIN_MOVIMIENTOS_ZSCORE = 5

# Umbral de |z| a partir del cual un movimiento se considera atípico.
UMBRAL_Z_SCORE = 2.0


@dataclass
class HallazgoAnomalia:
    """Resultado de un detector, listo para convertirse en un registro
    Anomalia (lo hace el comando "detectar_anomalias")."""
    producto_id: int
    fecha_deteccion: datetime
    tipo: str
    severidad: str
    score: float
    valor_observado: float
    valor_esperado: float
    descripcion: str
    # Solo lo trae detectar_movimientos_atipicos: qué Movimiento puntual
    # disparó el hallazgo, para poder enlazarlo desde el listado de
    # movimientos. detectar_diferencias_inventario nace de un ConteoDetalle,
    # no de un movimiento, así que lo deja en None.
    movimiento_id: int = None
    # Solo lo trae detectar_diferencias_inventario: la línea del conteo
    # físico que originó el hallazgo.
    conteo_detalle_id: int = None


def _numero(valor, decimales=1):
    """Número con coma decimal, como se escribe en Perú (64.7 -> "64,7")."""
    return f'{valor:.{decimales}f}'.replace('.', ',')


def _severidad_diferencia(diferencia_relativa):
    """Alta si la diferencia llega a ANOMALIA_UMBRAL_ALTA del stock de
    sistema (100% por defecto) o lo supera, media desde
    ANOMALIA_UMBRAL_MEDIA (40%) hasta ese valor, baja por debajo. Es
    "mayor o igual" a propósito: un faltante nunca pasa del 100% (el conteo
    físico no baja de cero), así que con "mayor que" un producto que
    desapareció por completo quedaría como media. Los umbrales se leen de settings en
    cada llamada para poder ajustarlos desde .env sin tocar el código."""
    umbral_alta = settings.ANOMALIA_UMBRAL_ALTA
    umbral_media = settings.ANOMALIA_UMBRAL_MEDIA
    if not 0 < umbral_media < umbral_alta:
        raise ImproperlyConfigured(
            'Los umbrales de severidad deben cumplir 0 < ANOMALIA_UMBRAL_MEDIA < '
            f'ANOMALIA_UMBRAL_ALTA (hoy: {umbral_media} y {umbral_alta}).'
        )
    if diferencia_relativa >= umbral_alta:
        return Anomalia.Severidad.ALTA
    if diferencia_relativa >= umbral_media:
        return Anomalia.Severidad.MEDIA
    return Anomalia.Severidad.BAJA


def detectar_diferencias_inventario(origen=None):
    """Corre el detector de diferencias de inventario sobre ConteoDetalle y
    devuelve una lista de HallazgoAnomalia (uno por cada ConteoDetalle
    marcado como anómalo)."""
    qs = ConteoDetalle.objects.select_related('producto', 'conteo')
    if origen:
        qs = qs.filter(conteo__origen=origen)
    detalles = list(qs)
    if not detalles:
        return []

    diferencias_abs = np.array([abs(d.diferencia) for d in detalles], dtype=float)
    # Stock de sistema en 0 se trata como 1 para el denominador: evita la
    # división por cero, y cualquier diferencia sobre un stock "en cero" ya
    # la captura de sobra la regla del 20%.
    stocks_sistema = np.array([max(d.stock_sistema, 1) for d in detalles], dtype=float)
    diferencias_relativas = diferencias_abs / stocks_sistema

    if len(detalles) >= MIN_REGISTROS_ISOLATION_FOREST:
        X = np.column_stack([diferencias_abs, diferencias_relativas])
        modelo = IsolationForest(random_state=42, contamination='auto')
        modelo.fit(X)
        es_outlier_if = modelo.predict(X) == -1
        # decision_function: más bajo (más negativo) = más anómalo. Se
        # invierte el signo para que el score de Anomalia sea "más alto =
        # más anómalo", igual que el z-score del otro detector.
        scores = -modelo.decision_function(X)
    else:
        es_outlier_if = np.zeros(len(detalles), dtype=bool)
        scores = np.zeros(len(detalles), dtype=float)

    ahora = timezone.now()
    hallazgos = []
    for i, detalle in enumerate(detalles):
        pasa_regla_20_por_ciento = diferencias_relativas[i] > UMBRAL_DIFERENCIA_RELATIVA
        if not (pasa_regla_20_por_ciento or es_outlier_if[i]):
            continue

        # Sin el producto: la pantalla ya lo muestra en su propia columna.
        porcentaje = (
            f'{_numero(diferencias_relativas[i] * 100)}%' if detalle.stock_sistema > 0
            else 'sin stock en sistema'
        )
        descripcion = (
            f'Conteo del {detalle.conteo.fecha_corte:%d/%m}: {detalle.stock_fisico} físicas contra '
            f'{detalle.stock_sistema} registradas ({detalle.diferencia:+d}, {porcentaje})'
        )
        hallazgos.append(HallazgoAnomalia(
            producto_id=detalle.producto_id,
            fecha_deteccion=ahora,
            tipo=Anomalia.Tipo.DIFERENCIA_INVENTARIO,
            severidad=_severidad_diferencia(diferencias_relativas[i]),
            score=float(scores[i]),
            valor_observado=float(detalle.diferencia),
            valor_esperado=0.0,
            descripcion=descripcion,
            conteo_detalle_id=detalle.pk,
        ))
    return hallazgos


def _severidad_z_score(z_abs):
    """Alta si el movimiento se aleja más de 3.5 desviaciones estándar de lo
    habitual, media si supera 2.5, baja si solo supera el umbral de 2.0."""
    if z_abs >= 3.5:
        return Anomalia.Severidad.ALTA
    if z_abs >= 2.5:
        return Anomalia.Severidad.MEDIA
    return Anomalia.Severidad.BAJA


def detectar_movimientos_atipicos(origen=None):
    """Corre el detector de movimientos atípicos sobre Movimiento (solo
    ingresos y salidas) y devuelve (hallazgos, sin_historico_suficiente):

    - hallazgos: lista de HallazgoAnomalia.
    - sin_historico_suficiente: lista de (producto_id, tipo, n_movimientos)
      para los grupos producto/tipo que no llegaron a
      MIN_MOVIMIENTOS_ZSCORE movimientos y por lo tanto no se evaluaron.
    """
    qs = Movimiento.objects.select_related('producto').filter(
        tipo__in=[Movimiento.Tipo.INGRESO, Movimiento.Tipo.SALIDA],
    )
    if origen:
        qs = qs.filter(origen=origen)

    grupos = {}
    for movimiento in qs:
        clave = (movimiento.producto_id, movimiento.tipo)
        grupos.setdefault(clave, []).append(movimiento)

    ahora = timezone.now()
    hallazgos = []
    sin_historico_suficiente = []

    for (producto_id, tipo), movimientos in grupos.items():
        if len(movimientos) < MIN_MOVIMIENTOS_ZSCORE:
            sin_historico_suficiente.append((producto_id, tipo, len(movimientos)))
            continue

        cantidades = np.array([m.cantidad for m in movimientos], dtype=float)
        media = float(cantidades.mean())
        desviacion = float(cantidades.std(ddof=1))
        if desviacion == 0:
            continue  # sin variabilidad: ningún valor puede ser "atípico"

        for movimiento, cantidad in zip(movimientos, cantidades):
            z = (cantidad - media) / desviacion
            if abs(z) < UMBRAL_Z_SCORE:
                continue

            descripcion = (
                f'{movimiento.get_tipo_display()} del {timezone.localtime(movimiento.fecha):%d/%m}: '
                f'{movimiento.cantidad} u. contra un promedio de {_numero(media)} (z = {_numero(z, 2)})'
            )
            hallazgos.append(HallazgoAnomalia(
                producto_id=producto_id,
                fecha_deteccion=ahora,
                tipo=Anomalia.Tipo.MOVIMIENTO_ATIPICO,
                severidad=_severidad_z_score(abs(z)),
                score=float(abs(z)),
                valor_observado=float(cantidad),
                valor_esperado=media,
                descripcion=descripcion,
                movimiento_id=movimiento.pk,
            ))

    return hallazgos, sin_historico_suficiente
