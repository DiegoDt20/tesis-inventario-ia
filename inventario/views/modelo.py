"""Pantalla técnica del modelo de predicción de demanda: panel del modelo
activo con todas sus métricas, comparación de los tres modelos (línea base
ingenua, solo interno, preentrenado + ajustado) e historial completo de
entrenamientos. Documentación de la investigación para la tesis, no una
pantalla de uso diario del negocio — por eso vive aparte del dashboard."""
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Max, Min
from django.shortcuts import render

from ..models import ModeloEntrenado, Origen, PedidoDetalle


def _estado_modelo_activo(origen):
    """Panel de estado del modelo activo: prioriza el ajustado (transferencia
    ya aplicada) y cae al base si todavía no hay uno ajustado."""
    modelo = (
        ModeloEntrenado.objects.filter(fase=ModeloEntrenado.Fase.AJUSTADO, activo=True).first()
        or ModeloEntrenado.objects.filter(fase=ModeloEntrenado.Fase.BASE, activo=True).first()
    )

    detalles_qs = PedidoDetalle.objects.all()
    if origen:
        detalles_qs = detalles_qs.filter(pedido__origen=origen)
    rango = detalles_qs.aggregate(
        minimo=Min('pedido__fecha_solicitud'), maximo=Max('pedido__fecha_solicitud'),
    )
    dias_historico = None
    if rango['minimo'] and rango['maximo']:
        dias_historico = (rango['maximo'].date() - rango['minimo'].date()).days + 1

    # Para no inducir a error: las métricas de un modelo "base" se
    # midieron sobre el dataset externo de Kaggle, no sobre la
    # microempresa; solo las de un modelo "ajustado" son sobre datos
    # internos reales.
    fuente_metricas = None
    if modelo and modelo.fase == ModeloEntrenado.Fase.BASE:
        fuente_metricas = (
            'Estas métricas se midieron sobre el dataset externo de Kaggle '
            '(Store Item Demand Forecasting Challenge), no sobre datos de la microempresa.'
        )
    elif modelo and modelo.fase == ModeloEntrenado.Fase.AJUSTADO:
        if modelo.origen_datos_internos:
            fuente_metricas = (
                'Estas métricas se midieron sobre los datos internos de la microempresa '
                f'(origen: {modelo.get_origen_datos_internos_display()}).'
            )
        else:
            fuente_metricas = 'Estas métricas se midieron sobre los datos internos de la microempresa.'

    return {'modelo': modelo, 'dias_historico': dias_historico, 'fuente_metricas': fuente_metricas}


# (campo, etiqueta, True si "más alto es mejor"). R² es la única métrica
# donde gana el valor más alto; en MAE, RMSE y SMAPE gana el más bajo.
METRICAS_COMPARACION = [
    ('mae', 'MAE', False),
    ('rmse', 'RMSE', False),
    ('smape', 'SMAPE', False),
    ('r2', 'R²', True),
]


def _comparacion_tres_modelos(modelo):
    """Filas de la comparación de los tres modelos (línea base, solo
    interno, preentrenado + ajustado) con sus cuatro métricas, marcando en
    cada métrica qué modelo gana. Un valor None (modelos ajustados
    entrenados antes de que se guardaran esas métricas) se muestra como
    guion y no compite. Devuelve None si el modelo no tiene comparación."""
    if modelo is None or modelo.mae_linea_base is None:
        return None

    filas = [
        {'nombre': 'Línea base (ingenua)', 'es_activo': False,
         'valores': {c: getattr(modelo, f'{c}_linea_base') for c, _, _ in METRICAS_COMPARACION}},
        {'nombre': 'Solo datos internos', 'es_activo': False,
         'valores': {c: getattr(modelo, f'{c}_solo_interno') for c, _, _ in METRICAS_COMPARACION}},
        {'nombre': 'Preentrenado + ajustado', 'es_activo': True,
         'valores': {c: getattr(modelo, c) for c, _, _ in METRICAS_COMPARACION}},
    ]
    ganadores = {}
    for campo, _, mayor_es_mejor in METRICAS_COMPARACION:
        candidatos = [(i, f['valores'][campo]) for i, f in enumerate(filas) if f['valores'][campo] is not None]
        if len(candidatos) > 1:
            elegir = max if mayor_es_mejor else min
            ganadores[campo] = elegir(candidatos, key=lambda par: par[1])[0]

    for i, fila in enumerate(filas):
        fila['celdas'] = [
            {'valor': fila['valores'][campo], 'gana': ganadores.get(campo) == i,
             'decimales': 4 if campo in ('smape', 'r2') else 2}
            for campo, _, _ in METRICAS_COMPARACION
        ]
    return {'metricas': [etiqueta for _, etiqueta, _ in METRICAS_COMPARACION], 'filas': filas}


@login_required
def modelo_detalle(request):
    estado_modelo = _estado_modelo_activo(Origen.REAL)
    pagina = Paginator(ModeloEntrenado.objects.order_by('-fecha_entrenamiento'), 20).get_page(request.GET.get('page'))
    contexto = {
        'estado_modelo': estado_modelo,
        'comparacion': _comparacion_tres_modelos(estado_modelo['modelo']),
        'pagina': pagina,
    }
    return render(request, 'inventario/modelo.html', contexto)
