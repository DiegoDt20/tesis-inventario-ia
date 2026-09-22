"""Pantalla del asistente conversacional (RAG + LLM local vía Ollama)."""
import json

from django.contrib.auth.decorators import login_required
from django.http import StreamingHttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from ..asistente.asistente import consultar_asistente, consultar_asistente_stream
from ..models import ConsultaAsistente

SESSION_HISTORIAL_ASISTENTE = 'asistente_historial'


@login_required
def asistente_chat(request):
    """Chat simple con historial en sesión (no en base de datos: se pierde
    al cerrar sesión o limpiar). Cada consulta además queda registrada de
    forma permanente en ConsultaAsistente, con los documentos usados, para
    poder auditarla y para la discusión de la tesis."""
    historial = request.session.get(SESSION_HISTORIAL_ASISTENTE, [])

    if request.method == 'POST':
        pregunta = request.POST.get('pregunta', '').strip()
        if pregunta:
            resultado = consultar_asistente(pregunta)
            documentos_usados = [
                {
                    'tipo': documento.get_tipo_display(),
                    'referencia_id': documento.referencia_id,
                    'contenido': documento.contenido,
                }
                for documento in resultado.documentos
            ]

            ConsultaAsistente.objects.create(
                pregunta=pregunta,
                respuesta=resultado.respuesta,
                documentos_usados=documentos_usados,
                usuario=request.user,
            )

            historial.append({
                'pregunta': pregunta,
                'respuesta': resultado.respuesta,
                'documentos': documentos_usados,
                'fallo': resultado.fallo,
            })
            request.session[SESSION_HISTORIAL_ASISTENTE] = historial
        return redirect('inventario:asistente_chat')

    return render(request, 'inventario/asistente_chat.html', {'historial': historial})


@login_required
@require_POST
def asistente_limpiar(request):
    request.session.pop(SESSION_HISTORIAL_ASISTENTE, None)
    return redirect('inventario:asistente_chat')


def _evento_sse(nombre, datos):
    """Un evento de Server-Sent Events: 'event: nombre' + 'data: <json>',
    con el payload en una sola línea de JSON (el formato SSE no admite
    saltos de línea crudos dentro de un campo 'data')."""
    return f'event: {nombre}\ndata: {json.dumps(datos)}\n\n'


@login_required
def asistente_stream(request):
    """Respuesta del asistente en tiempo real: Server-Sent Events con un
    evento 'fragmento' por cada trozo de texto que Ollama va generando
    (stream=True en proveedores.py) y un evento final 'fin' con el texto
    completo y las fuentes. GET porque EventSource (la API del navegador
    para SSE) solo puede hacer peticiones GET.

    No actualiza el historial en sesión (SESSION_HISTORIAL_ASISTENTE): con
    una StreamingHttpResponse, el middleware de sesión ya guardó la sesión
    antes de que este generador se ejecute, así que escribir en
    request.session aquí no se guardaría de forma confiable. El registro
    que sí importa para auditoría (ConsultaAsistente) es un guardado en
    base de datos normal, y ese sí ocurre de forma fiable durante el
    streaming. El historial visual de esta conversación lo lleva el
    JavaScript de la pantalla (asistente_chat.html); un recargado de página
    empieza un hilo visual nuevo, pero la consulta queda igual auditada.
    """
    pregunta = request.GET.get('pregunta', '').strip()

    def generador():
        if not pregunta:
            yield _evento_sse('fin', {'respuesta': '', 'documentos': [], 'fallo': True})
            return

        documentos_usados = []
        respuesta_completa = ''
        fallo = False
        for evento in consultar_asistente_stream(pregunta):
            if evento['tipo'] == 'fragmento':
                yield _evento_sse('fragmento', {'texto': evento['texto']})
            else:
                respuesta_completa = evento['respuesta']
                fallo = evento['fallo']
                documentos_usados = [
                    {
                        'tipo': documento.get_tipo_display(),
                        'referencia_id': documento.referencia_id,
                        'contenido': documento.contenido,
                    }
                    for documento in evento['documentos']
                ]
                ConsultaAsistente.objects.create(
                    pregunta=pregunta, respuesta=respuesta_completa,
                    documentos_usados=documentos_usados, usuario=request.user,
                )
                yield _evento_sse('fin', {
                    'respuesta': respuesta_completa, 'documentos': documentos_usados, 'fallo': fallo,
                })

    respuesta = StreamingHttpResponse(generador(), content_type='text/event-stream')
    respuesta['Cache-Control'] = 'no-cache'
    # nginx (si lo hay delante) no debe bufferizar esto: sin este header
    # los fragmentos llegarían todos juntos al final, no en tiempo real.
    respuesta['X-Accel-Buffering'] = 'no'
    return respuesta
