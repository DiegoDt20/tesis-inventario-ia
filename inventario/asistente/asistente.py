"""Arma el prompt del asistente conversacional (pregunta del usuario más el
contexto recuperado por RAG), llama al proveedor de LLM configurado y
devuelve la respuesta.

Regla que manda sobre todo: el modelo de lenguaje NUNCA calcula ni inventa
cifras. Solo redacta en lenguaje natural los datos que ya calculó el
sistema (indicadores, recomendaciones, anomalías...). Si la pregunta
requiere un dato que el contexto recuperado no tiene, el modelo debe decir
que no lo tiene, no estimarlo.
"""
from .anonimizador import anonimizar_texto
from .proveedores import ErrorProveedorLLM, obtener_proveedor
from .recuperador import recuperar_documentos

# Empieza fijando el idioma explícitamente: qwen2.5 (el modelo local
# configurado) tiende a mezclar otros idiomas a mitad de la respuesta si no
# se le indica esto primero y de forma explícita.
PROMPT_SISTEMA = (
    'Responde SIEMPRE en español, nunca en otro idioma, sin mezclar '
    'palabras de otros idiomas, incluso si el contexto o la pregunta las '
    'contienen.\n\n'
    'Eres el asistente de un sistema de gestión de inventario. Respondes '
    'ÚNICAMENTE con la información que aparece en el CONTEXTO proporcionado '
    'a continuación. No calcules, estimes ni inventes ninguna cifra por tu '
    'cuenta: todos los números del contexto ya fueron calculados por el '
    'sistema. Si la pregunta requiere un dato que no aparece en el '
    'contexto, responde exactamente que no cuentas con ese dato en vez de '
    'estimarlo o suponerlo. Responde de forma breve y clara.\n\n'
    'Si te preguntan por recomendaciones de reposición, NO enumeres '
    'producto por producto los que no requieren reposición: el contexto ya '
    'trae ese conteo agregado (por ejemplo "N no requieren reposición"), '
    'úsalo tal cual. Detalla individualmente solo los productos que sí '
    'requieren una acción (crítico o para reponer), con su cantidad '
    'sugerida.'
)


class RespuestaAsistente:
    """Resultado de consultar_asistente().

    `documentos`: los DocumentoIndexado recuperados (siempre, haya fallado
    o no el proveedor de LLM, para poder auditar o mostrar el contexto
    crudo). `fallo`: True si el proveedor de LLM no respondió; en ese caso
    `respuesta` ya trae armado un texto con el contexto sin redactar (ver
    _mensaje_contexto_crudo), para no dejar la pantalla sin nada que
    mostrar."""

    def __init__(self, respuesta, documentos, fallo=False):
        self.respuesta = respuesta
        self.documentos = documentos
        self.fallo = fallo


def _construir_contexto(documentos):
    fragmentos = [anonimizar_texto(doc.contenido) for doc in documentos]
    return '\n'.join(f'- {fragmento}' for fragmento in fragmentos)


def _mensaje_contexto_crudo(documentos):
    fragmentos = '\n'.join(f'- {doc.contenido}' for doc in documentos)
    return (
        'No se pudo contactar al servicio de IA para redactar la respuesta. '
        'Estos son los datos recuperados, sin redactar:\n' + fragmentos
    )


def consultar_asistente(pregunta, n_documentos=5):
    """Punto de entrada del asistente conversacional. Nunca lanza una
    excepción por una falla del proveedor de LLM: la refleja en
    RespuestaAsistente.fallo para que la pantalla pueda mostrar el contexto
    recuperado en su lugar, sin quedar inutilizable."""
    documentos = recuperar_documentos(pregunta, n=n_documentos)

    if not documentos:
        return RespuestaAsistente(
            respuesta='Todavía no hay información indexada para responder preguntas. '
                      'Pide a un administrador que corra el comando "indexar_conocimiento".',
            documentos=[],
        )

    contexto = _construir_contexto(documentos)
    pregunta_anonimizada = anonimizar_texto(pregunta)
    mensajes = [
        {'role': 'system', 'content': PROMPT_SISTEMA},
        {'role': 'user', 'content': f'CONTEXTO:\n{contexto}\n\nPREGUNTA: {pregunta_anonimizada}'},
    ]

    try:
        respuesta = obtener_proveedor().generar_respuesta(mensajes)
    except ErrorProveedorLLM:
        return RespuestaAsistente(
            respuesta=_mensaje_contexto_crudo(documentos), documentos=documentos, fallo=True,
        )

    return RespuestaAsistente(respuesta=respuesta, documentos=documentos)
