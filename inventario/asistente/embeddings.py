"""Modelo de embeddings del índice RAG del asistente conversacional.

Modelo multilingüe (entiende español) que corre localmente con
sentence-transformers: generar un embedding no implica ninguna llamada a una
API externa, a diferencia de la redacción de la respuesta (ver
proveedores.py), que sí depende del proveedor de LLM configurado.
"""
from functools import lru_cache

from sentence_transformers import SentenceTransformer

# paraphrase-multilingual-MiniLM-L12-v2: liviano, soporta español y es
# suficiente para el volumen de documentos de una microempresa.
NOMBRE_MODELO = 'paraphrase-multilingual-MiniLM-L12-v2'
DIMENSIONES = 384


@lru_cache(maxsize=1)
def _modelo():
    """Carga el modelo una sola vez por proceso: instanciar
    SentenceTransformer es costoso (descarga/lee los pesos del modelo)."""
    return SentenceTransformer(NOMBRE_MODELO)


def generar_embedding(texto):
    """Devuelve el embedding de `texto` (lista de DIMENSIONES floats),
    generado localmente."""
    return _modelo().encode(texto, normalize_embeddings=True).tolist()
