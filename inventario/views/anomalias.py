"""Pantalla de anomalías de control de existencias detectadas por
inventario/ml/anomalias.py: filtradas por severidad, tipo y estado de
revisión, agrupadas por conteo físico, y revisables de a una o en bloque con
un motivo opcional (la causa del descuadre, que es lo que el objetivo 1
busca corregir)."""
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.paginator import Paginator
from django.db.models import Case, Count, F, IntegerField, Q, When
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..models import Anomalia

ANOMALIAS_POR_PAGINA = 50

ESTADOS_REVISION = [
    ('pendientes', 'Sin revisar'),
    ('revisadas', 'Revisadas'),
    ('todas', 'Todas'),
]

ORDENES = ('diferencia', '-diferencia')


def _orden_severidad():
    return Case(
        When(severidad=Anomalia.Severidad.ALTA, then=0),
        When(severidad=Anomalia.Severidad.MEDIA, then=1),
        default=2,
        output_field=IntegerField(),
    )


def _anomalias_no_revisadas(origen):
    """Anomalías (diferencias de inventario y movimientos atípicos) que
    todavía nadie marcó como revisadas, ordenadas por severidad (alta
    primero) y luego por fecha de detección más reciente."""
    qs = Anomalia.objects.filter(revisada=False)
    if origen:
        qs = qs.filter(producto__origen=origen)
    return list(
        qs.select_related('producto').annotate(orden_severidad=_orden_severidad())
        .order_by('orden_severidad', '-fecha_deteccion')
    )


def _leer_filtros(datos):
    """Filtros de la lista desde GET o POST; un valor desconocido se trata
    como "sin filtro" en vez de fallar."""
    severidad = datos.get('severidad', '')
    tipo = datos.get('tipo', '')
    estado = datos.get('estado', 'pendientes')
    orden = datos.get('orden', '')
    return {
        'severidad': severidad if severidad in Anomalia.Severidad.values else '',
        'tipo': tipo if tipo in Anomalia.Tipo.values else '',
        'estado': estado if estado in dict(ESTADOS_REVISION) else 'pendientes',
        'orden': orden if orden in ORDENES else '',
    }


def _filtrar(filtros):
    qs = Anomalia.objects.all()
    if filtros['severidad']:
        qs = qs.filter(severidad=filtros['severidad'])
    if filtros['tipo']:
        qs = qs.filter(tipo=filtros['tipo'])
    if filtros['estado'] == 'pendientes':
        qs = qs.filter(revisada=False)
    elif filtros['estado'] == 'revisadas':
        qs = qs.filter(revisada=True)
    return qs


def _ordenar(qs, orden):
    """Primero por conteo (fecha de corte más reciente arriba; los
    movimientos atípicos, sin conteo, al final), para que cada grupo quede
    contiguo. Dentro del grupo, por la diferencia si se pidió; si no, por
    severidad y luego por la diferencia más grande en valor absoluto."""
    grupo = [
        F('conteo_detalle__conteo__fecha_corte').desc(nulls_last=True),
        F('conteo_detalle__conteo_id').desc(nulls_last=True),
    ]
    if orden == 'diferencia':
        dentro = [F('conteo_detalle__diferencia').asc(nulls_last=True)]
    elif orden == '-diferencia':
        dentro = [F('conteo_detalle__diferencia').desc(nulls_last=True)]
    else:
        dentro = ['orden_severidad', F('score').desc()]
    return (
        qs.select_related('producto', 'conteo_detalle__conteo')
        .annotate(orden_severidad=_orden_severidad())
        .order_by(*grupo, *dentro, 'pk')
    )


def _agrupar_por_conteo(anomalias, totales):
    """Arma los grupos de la página: uno por conteo físico (y uno aparte
    para los movimientos atípicos). `totales` trae, por conteo, el total de
    anomalías filtradas de todo el conteo, no solo las de esta página."""
    grupos = []
    for anomalia in anomalias:
        conteo = anomalia.conteo_detalle.conteo if anomalia.conteo_detalle_id else None
        clave = conteo.pk if conteo else None
        if not grupos or grupos[-1]['clave'] != clave:
            grupos.append({'clave': clave, 'conteo': conteo, 'anomalias': [], **totales.get(clave, {})})
        grupos[-1]['anomalias'].append(anomalia)
    return grupos


def _totales_por_conteo(qs):
    filas = qs.values('conteo_detalle__conteo_id').annotate(
        total=Count('id'),
        altas=Count('id', filter=Q(severidad=Anomalia.Severidad.ALTA)),
        medias=Count('id', filter=Q(severidad=Anomalia.Severidad.MEDIA)),
        bajas=Count('id', filter=Q(severidad=Anomalia.Severidad.BAJA)),
    )
    return {
        f['conteo_detalle__conteo_id']: {k: f[k] for k in ('total', 'altas', 'medias', 'bajas')}
        for f in filas
    }


def _motivos_registrados():
    """Cuántas anomalías revisadas hay por motivo: la lectura de por qué se
    descuadra el inventario."""
    etiquetas = dict(Anomalia.MotivoRevision.choices)
    filas = (
        Anomalia.objects.filter(revisada=True).exclude(motivo_revision='')
        .values('motivo_revision').annotate(total=Count('id')).order_by('-total')
    )
    return [{'etiqueta': etiquetas[f['motivo_revision']], 'total': f['total']} for f in filas]


def _querystring(filtros, **cambios):
    datos = {**filtros, **cambios}
    return urlencode({k: v for k, v in datos.items() if v and not (k == 'estado' and v == 'pendientes')})


def _contexto_lista(filtros, numero_pagina):
    qs = _filtrar(filtros)
    pagina = Paginator(_ordenar(qs, filtros['orden']), ANOMALIAS_POR_PAGINA).get_page(numero_pagina)
    # Clic en "Diferencia": sin orden -> de mayor faltante a mayor sobrante
    # -> al revés -> vuelve al orden por severidad.
    siguiente_orden = {'': 'diferencia', 'diferencia': '-diferencia', '-diferencia': ''}[filtros['orden']]
    return {
        'pagina': pagina,
        'grupos': _agrupar_por_conteo(pagina.object_list, _totales_por_conteo(qs)),
        'total_filtradas': pagina.paginator.count,
        'filtros': filtros,
        'querystring': _querystring(filtros),
        'querystring_orden_diferencia': _querystring(filtros, orden=siguiente_orden),
        'severidades': Anomalia.Severidad.choices,
        'tipos': Anomalia.Tipo.choices,
        'estados': ESTADOS_REVISION,
        'motivos': Anomalia.MotivoRevision.choices,
        'motivos_registrados': _motivos_registrados(),
    }


def _render_lista(request, filtros, numero_pagina):
    plantilla = (
        'inventario/_anomalias_lista_resultados.html' if request.headers.get('HX-Request') == 'true'
        else 'inventario/anomalias_lista.html'
    )
    return render(request, plantilla, _contexto_lista(filtros, numero_pagina))


@login_required
@permission_required('inventario.change_anomalia', raise_exception=True)
def anomalias_lista(request):
    return _render_lista(request, _leer_filtros(request.GET), request.GET.get('page'))


def _datos_revision(request):
    """Motivo y detalle opcionales de la revisión. En la revisión de una
    sola anomalía con motivo "Otro", el detalle llega por hx-prompt."""
    motivo = request.POST.get('motivo', '')
    if motivo not in Anomalia.MotivoRevision.values:
        motivo = ''
    detalle = request.POST.get('detalle') or request.headers.get('HX-Prompt') or ''
    return {
        'revisada': True,
        'fecha_revision': timezone.now(),
        'motivo_revision': motivo,
        'detalle_revision': detalle.strip()[:200],
    }


def _responder_tras_revisar(request, cantidad):
    """Con htmx, vuelve a pintar la lista con los mismos filtros y página
    (así se actualizan también los totales de cada conteo); sin htmx,
    redirige con un mensaje."""
    if request.headers.get('HX-Request') == 'true':
        return _render_lista(request, _leer_filtros(request.POST), request.POST.get('page'))
    if cantidad == 1:
        messages.success(request, 'Anomalía marcada como revisada.')
    else:
        messages.success(request, f'{cantidad} anomalías marcadas como revisadas.')
    return redirect('inventario:anomalias_lista')


@login_required
@permission_required('inventario.change_anomalia', raise_exception=True)
@require_POST
def anomalia_marcar_revisada(request, pk):
    anomalia = get_object_or_404(Anomalia, pk=pk)
    for campo, valor in _datos_revision(request).items():
        setattr(anomalia, campo, valor)
    anomalia.save(update_fields=['revisada', 'fecha_revision', 'motivo_revision', 'detalle_revision'])
    return _responder_tras_revisar(request, 1)


@login_required
@permission_required('inventario.change_anomalia', raise_exception=True)
@require_POST
def anomalias_marcar_revisadas(request):
    """Marca como revisadas, en bloque, las anomalías seleccionadas en la
    lista, todas con el mismo motivo (opcional). Las que ya estaban
    revisadas no se tocan: no se pisa su motivo ni su fecha de revisión."""
    ids = [int(i) for i in request.POST.getlist('ids') if i.isdigit()]
    if not ids and request.headers.get('HX-Request') != 'true':
        messages.warning(request, 'No seleccionaste ninguna anomalía.')
        return redirect('inventario:anomalias_lista')
    cantidad = Anomalia.objects.filter(pk__in=ids, revisada=False).update(**_datos_revision(request))
    return _responder_tras_revisar(request, cantidad)
