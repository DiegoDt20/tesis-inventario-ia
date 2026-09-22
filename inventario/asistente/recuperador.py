"""Recuperación de documentos relevantes para una pregunta del usuario: dada
la pregunta, genera su embedding (localmente, ver embeddings.py) y devuelve
los N documentos más similares por distancia coseno, usando pgvector.

Para preguntas generales sobre el estado del negocio ("¿cómo va el
inventario?", "resumen del negocio"), la similitud semántica pura tiende a
traer fichas de producto sueltas y poco representativas (nada en la
pregunta apunta a un producto en particular). Para esos casos se priorizan
los documentos agregados (indicadores, resúmenes por categoría y de
estados, resumen de anomalías) y las recomendaciones críticas por encima de
las fichas de producto individuales.

Para preguntas sobre recomendaciones/reposición que no califican como
generales (p. ej. "¿qué debo reponer?"), siempre se incluye el resumen de
estados: sin el conteo de "no requieren reposición" que trae ese
documento, el modelo puede intentar inventarlo o calcularlo por su cuenta.
"""
from django.db.models import Q
from pgvector.django import CosineDistance

from inventario.models import DocumentoIndexado, Recomendacion, TipoDocumento

from .embeddings import generar_embedding

N_DOCUMENTOS_DEFECTO = 5

# Frases que delatan una pregunta general sobre el negocio, no sobre un
# producto puntual. Coincidencia por substring, sin distinguir mayúsculas:
# simple a propósito, es solo para decidir qué priorizar, no para entender
# la pregunta.
FRASES_PREGUNTA_GENERAL = (
    'estado del inventario', 'estado general', 'cómo va', 'como va',
    'cómo está', 'como esta', 'resumen', 'panorama', 'visión general',
    'vision general', 'negocio', 'en general', 'situación general',
    'situacion general', 'de un vistazo',
)

# Documentos agregados que en una pregunta general se incluyen SIEMPRE
# completos, sin recortar a `n`: son pocos (uno por categoría, más uno de
# indicadores, uno de estados y uno de anomalías), y recortarlos por
# cercanía puede dejar fuera una categoría entera (p. ej. "Esmalte", la más
# grande, si su embedding no queda entre los N más cercanos a la pregunta).
TIPOS_RESUMEN_COMPLETO = (
    TipoDocumento.INDICADOR,
    TipoDocumento.RESUMEN_CATEGORIA,
    TipoDocumento.RESUMEN_ESTADO,
    TipoDocumento.RESUMEN_ANOMALIAS,
)

# A diferencia de los de arriba, las recomendaciones críticas NO tienen un
# número acotado (puede haber decenas): el total exacto ya lo dice
# _documento_resumen_estado, así que acá solo se detallan las más
# relevantes para la pregunta, no todas, para no inundar el contexto.
MAX_RECOMENDACIONES_CRITICAS_DETALLE = 5

# Frases que delatan una pregunta sobre recomendaciones/reposición, aunque
# no sea "general" en el sentido de arriba (p. ej. "¿qué debo reponer?").
# Sin este documento en el contexto, el modelo se queda sin el conteo de
# "no requieren reposición" y puede intentar inventarlo o calcularlo por su
# cuenta a partir de otros números — algo que tiene prohibido.
FRASES_PREGUNTA_RECOMENDACIONES = (
    'recomendacion', 'recomendación', 'recomendaciones', 'reponer', 'reposicion',
    'reposición', 'qué pedir', 'que pedir', 'qué comprar', 'que comprar',
    'orden de compra', 'ordenes de compra', 'órdenes de compra',
)


def _es_pregunta_general(pregunta):
    texto = pregunta.lower()
    return any(frase in texto for frase in FRASES_PREGUNTA_GENERAL)


def _es_pregunta_sobre_recomendaciones(pregunta):
    texto = pregunta.lower()
    return any(frase in texto for frase in FRASES_PREGUNTA_RECOMENDACIONES)


def _ids_recomendaciones_criticas():
    """PKs de Recomendacion en estado crítico del último lote generado: es
    la única recomendación que aporta más que la ficha del producto en una
    pregunta general (una recomendación "normal" no dice nada que la ficha
    no diga ya)."""
    ultima_fecha = (
        Recomendacion.objects.order_by('-fecha_generacion').values_list('fecha_generacion', flat=True).first()
    )
    if ultima_fecha is None:
        return set()
    return set(
        Recomendacion.objects.filter(
            fecha_generacion=ultima_fecha, estado=Recomendacion.Estado.CRITICO,
        ).values_list('pk', flat=True)
    )


def _recuperar_priorizando_resumenes(qs, n):
    """Arma el contexto de una pregunta general: primero TODOS los
    documentos de resumen (nunca recortados: son pocos y omitir uno rompe
    el resumen, p. ej. dejaría una categoría sin contar), luego hasta
    MAX_RECOMENDACIONES_CRITICAS_DETALLE recomendaciones críticas del
    último lote (el total exacto ya lo dice el resumen de estados), y solo
    si sobra espacio hasta `n` se completa con el resto de documentos por
    cercanía."""
    resumen = list(qs.filter(tipo__in=TIPOS_RESUMEN_COMPLETO).order_by('tipo', 'distancia'))
    ids_usados = [doc.pk for doc in resumen]

    ids_criticas = _ids_recomendaciones_criticas()
    criticas = []
    if ids_criticas:
        criticas = list(
            qs.filter(tipo=TipoDocumento.RECOMENDACION, referencia_id__in=ids_criticas)
            .exclude(pk__in=ids_usados).order_by('distancia')[:MAX_RECOMENDACIONES_CRITICAS_DETALLE]
        )
        ids_usados += [doc.pk for doc in criticas]

    resultado = resumen + criticas
    if len(resultado) >= n:
        return resultado

    faltantes = n - len(resultado)
    resto = list(qs.exclude(pk__in=ids_usados).order_by('distancia')[:faltantes])
    return resultado + resto


def _recuperar_priorizando_recomendaciones(qs, n):
    """El resumen de estados (con el conteo de "no requieren reposición")
    entra siempre al contexto de una pregunta sobre recomendaciones; el
    resto del cupo se llena con las recomendaciones accionables más
    cercanas a la pregunta."""
    resumen_estado = list(qs.filter(tipo=TipoDocumento.RESUMEN_ESTADO))
    ids_usados = [doc.pk for doc in resumen_estado]

    faltantes = max(n - len(resumen_estado), 0)
    accionables = list(
        qs.filter(tipo=TipoDocumento.RECOMENDACION).exclude(pk__in=ids_usados)
        .order_by('distancia')[:faltantes]
    )
    return resumen_estado + accionables


def recuperar_documentos(pregunta, n=N_DOCUMENTOS_DEFECTO):
    """Devuelve los DocumentoIndexado más relevantes para `pregunta` (cada
    uno trae anotado `.distancia`), hasta `n` documentos en preguntas
    puntuales sobre un producto. En preguntas generales `n` es un mínimo,
    no un máximo: siempre se incluyen completos los documentos de resumen y
    hasta MAX_RECOMENDACIONES_CRITICAS_DETALLE recomendaciones críticas (ver
    _recuperar_priorizando_resumenes), aunque eso supere `n`. En preguntas
    sobre recomendaciones que no califican como generales, siempre se
    incluye el resumen de estados (ver _recuperar_priorizando_recomendaciones)."""
    embedding_pregunta = generar_embedding(pregunta)
    qs = DocumentoIndexado.objects.annotate(
        distancia=CosineDistance('embedding', embedding_pregunta),
    )

    if _es_pregunta_general(pregunta):
        return _recuperar_priorizando_resumenes(qs, n)

    if _es_pregunta_sobre_recomendaciones(pregunta):
        return _recuperar_priorizando_recomendaciones(qs, n)

    return list(qs.order_by('distancia')[:n])
