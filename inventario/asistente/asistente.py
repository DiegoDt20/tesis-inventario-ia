"""Arma el prompt del asistente conversacional (pregunta del usuario más el
contexto recuperado por RAG), llama al proveedor de LLM configurado y
devuelve la respuesta.

Regla que manda sobre todo: el modelo de lenguaje NUNCA calcula ni inventa
cifras. Solo redacta en lenguaje natural los datos que ya calculó el
sistema (indicadores, recomendaciones, anomalías...). Si la pregunta
requiere un dato que el contexto recuperado no tiene, el modelo debe decir
que no lo tiene, no estimarlo.
"""
from inventario.models import DocumentoIndexado

from .anonimizador import anonimizar_texto
from .configuracion import origen_asistente
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
    'Reporta las cifras sin calificarlas: no agregues valoraciones propias '
    'como "bajo", "alto", "bueno", "malo", "preocupante", "elevado" o '
    '"crítico", ni conclusiones sobre si un resultado está bien o mal. La '
    'interpretación le corresponde al usuario. Solo puedes usar una '
    'calificación si aparece tal cual en el CONTEXTO (por ejemplo, el estado '
    '"crítico" de una recomendación o la severidad "alta" de una anomalía, '
    'que ya asignó el sistema) o si el CONTEXTO trae el umbral o la meta '
    'con que compararla; en ese caso cita ese umbral.\n\n'
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
    mostrar. `proveedor`/`modelo`: con qué se generó la respuesta (ver
    _origen_respuesta); vacíos si no se llamó al LLM."""

    def __init__(self, respuesta, documentos, fallo=False, proveedor='', modelo=''):
        self.respuesta = respuesta
        self.documentos = documentos
        self.fallo = fallo
        self.proveedor = proveedor
        self.modelo = modelo


def _construir_contexto(documentos):
    fragmentos = [anonimizar_texto(doc.contenido) for doc in documentos]
    return '\n'.join(f'- {fragmento}' for fragmento in fragmentos)


def _origen_respuesta(proveedor):
    """(proveedor, modelo) para registrar en ConsultaAsistente: el modelo
    que informó el servicio si respondió, o el configurado si falló."""
    return proveedor.nombre, proveedor.modelo_respondio or proveedor.modelo


def _mensajes(documentos, pregunta):
    """Lo ÚNICO que se envía al proveedor de LLM: el prompt de sistema
    (texto fijo), el contexto recuperado anonimizado y la pregunta
    anonimizada. Nada más del sistema (ni el historial de la conversación,
    ni datos del usuario) sale hacia el modelo."""
    return [
        {'role': 'system', 'content': PROMPT_SISTEMA},
        {
            'role': 'user',
            'content': f'CONTEXTO:\n{_construir_contexto(documentos)}\n\nPREGUNTA: {anonimizar_texto(pregunta)}',
        },
    ]


def _mensaje_sin_indice():
    """Qué decir cuando no se recuperó ningún documento: o el índice está
    vacío, o se construyó con otro origen (ASISTENTE_ORIGEN cambió y nadie
    reindexó; el recuperador no mezcla documentos de otro origen)."""
    if DocumentoIndexado.objects.exists():
        return (
            f'El índice del asistente se construyó con otro origen de datos y ahora está '
            f'configurado "{origen_asistente()}". Pide a un administrador que corra el comando '
            '"indexar_conocimiento".'
        )
    return (
        'Todavía no hay información indexada para responder preguntas. '
        'Pide a un administrador que corra el comando "indexar_conocimiento".'
    )


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
        return RespuestaAsistente(respuesta=_mensaje_sin_indice(), documentos=[])

    proveedor = obtener_proveedor()
    try:
        respuesta = proveedor.generar_respuesta(_mensajes(documentos, pregunta))
        fallo = False
    except ErrorProveedorLLM:
        respuesta = _mensaje_contexto_crudo(documentos)
        fallo = True

    nombre, modelo = _origen_respuesta(proveedor)
    return RespuestaAsistente(
        respuesta=respuesta, documentos=documentos, fallo=fallo, proveedor=nombre, modelo=modelo,
    )


def consultar_asistente_stream(pregunta, n_documentos=5):
    """Igual que consultar_asistente, pero para la pantalla de respuesta en
    tiempo real (ver views/asistente.py:asistente_stream): en vez de
    devolver un RespuestaAsistente ya armado, es un generador que va
    produciendo eventos a medida que el modelo redacta.

    Eventos que produce (dicts):
      {'tipo': 'fragmento', 'texto': str} — un trozo más de la respuesta.
      {'tipo': 'fin', 'respuesta': str, 'documentos': [...], 'fallo': bool,
       'proveedor': str, 'modelo': str}
        — siempre el último evento, con el texto completo acumulado, las
        fuentes y con qué se generó, igual que RespuestaAsistente.

    Igual que consultar_asistente, nunca deja de producir el evento 'fin':
    una falla del proveedor de LLM se refleja en fallo=True, no en una
    excepción, para que la pantalla pueda cerrar el turno de todos modos."""
    documentos = recuperar_documentos(pregunta, n=n_documentos)

    if not documentos:
        mensaje = _mensaje_sin_indice()
        yield {'tipo': 'fragmento', 'texto': mensaje}
        yield {'tipo': 'fin', 'respuesta': mensaje, 'documentos': [], 'fallo': False, 'proveedor': '', 'modelo': ''}
        return

    proveedor = obtener_proveedor()
    texto_generado = []
    try:
        for fragmento in proveedor.generar_respuesta_stream(_mensajes(documentos, pregunta)):
            texto_generado.append(fragmento)
            yield {'tipo': 'fragmento', 'texto': fragmento}
    except ErrorProveedorLLM:
        nombre, modelo = _origen_respuesta(proveedor)
        if texto_generado:
            # Ya se alcanzó a mostrar algo antes de que el proveedor se
            # cayera: no se reemplaza (se perdería lo ya visto), se avisa
            # que la respuesta quedó cortada.
            aviso = '\n\n[Se perdió la conexión con el servicio de IA a mitad de la respuesta.]'
            yield {'tipo': 'fragmento', 'texto': aviso}
            yield {
                'tipo': 'fin', 'respuesta': ''.join(texto_generado) + aviso,
                'documentos': documentos, 'fallo': True, 'proveedor': nombre, 'modelo': modelo,
            }
        else:
            mensaje = _mensaje_contexto_crudo(documentos)
            yield {'tipo': 'fragmento', 'texto': mensaje}
            yield {
                'tipo': 'fin', 'respuesta': mensaje, 'documentos': documentos, 'fallo': True,
                'proveedor': nombre, 'modelo': modelo,
            }
        return

    nombre, modelo = _origen_respuesta(proveedor)
    yield {
        'tipo': 'fin', 'respuesta': ''.join(texto_generado), 'documentos': documentos, 'fallo': False,
        'proveedor': nombre, 'modelo': modelo,
    }
