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

# Documentos agregados que siempre pesan más que una ficha de producto en
# una pregunta general.
TIPOS_PRIORITARIOS_PREGUNTA_GENERAL = (
    TipoDocumento.INDICADOR,
    TipoDocumento.RESUMEN_CATEGORIA,
    TipoDocumento.RESUMEN_ESTADO,
    TipoDocumento.RESUMEN_ANOMALIAS,
)


def _es_pregunta_general(pregunta):
    texto = pregunta.lower()
    return any(frase in texto for frase in FRASES_PREGUNTA_GENERAL)


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
    filtro_prioritario = Q(tipo__in=TIPOS_PRIORITARIOS_PREGUNTA_GENERAL)
    ids_criticas = _ids_recomendaciones_criticas()
    if ids_criticas:
        filtro_prioritario |= Q(tipo=TipoDocumento.RECOMENDACION, referencia_id__in=ids_criticas)

    prioritarios = list(qs.filter(filtro_prioritario).order_by('distancia')[:n])
    if len(prioritarios) >= n:
        return prioritarios[:n]

    faltantes = n - len(prioritarios)
    resto = list(qs.exclude(pk__in=[doc.pk for doc in prioritarios]).order_by('distancia')[:faltantes])
    return prioritarios + resto


def recuperar_documentos(pregunta, n=N_DOCUMENTOS_DEFECTO):
    """Devuelve los `n` DocumentoIndexado más relevantes para `pregunta`
    (cada uno trae anotado `.distancia`). En preguntas generales, prioriza
    documentos agregados y recomendaciones críticas antes que rellenar con
    fichas de producto por similitud pura (ver _es_pregunta_general)."""
    embedding_pregunta = generar_embedding(pregunta)
    qs = DocumentoIndexado.objects.annotate(
        distancia=CosineDistance('embedding', embedding_pregunta),
    )

    if _es_pregunta_general(pregunta):
        return _recuperar_priorizando_resumenes(qs, n)

    return list(qs.order_by('distancia')[:n])
