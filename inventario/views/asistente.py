"""Pantalla del asistente conversacional (RAG + LLM local vía Ollama)."""
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from ..asistente.asistente import consultar_asistente
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
