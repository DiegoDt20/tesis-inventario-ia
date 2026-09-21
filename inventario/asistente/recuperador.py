"""Recuperación de documentos relevantes para una pregunta del usuario: dada
la pregunta, genera su embedding (localmente, ver embeddings.py) y devuelve
los N documentos más similares por distancia coseno, usando pgvector."""
from pgvector.django import CosineDistance

from inventario.models import DocumentoIndexado

from .embeddings import generar_embedding

N_DOCUMENTOS_DEFECTO = 5


def recuperar_documentos(pregunta, n=N_DOCUMENTOS_DEFECTO):
    """Devuelve los `n` DocumentoIndexado más similares a `pregunta` por
    distancia coseno entre embeddings, ordenados de más a menos similar
    (cada uno trae anotado `.distancia`)."""
    embedding_pregunta = generar_embedding(pregunta)
    return list(
        DocumentoIndexado.objects.annotate(
            distancia=CosineDistance('embedding', embedding_pregunta),
        ).order_by('distancia')[:n]
    )
