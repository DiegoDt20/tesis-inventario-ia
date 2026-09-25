"""Construye los documentos que alimentan el índice RAG del asistente
conversacional a partir de la base de datos: ficha de cada producto activo
con su stock/categoría/precio/demanda reciente, las recomendaciones
vigentes con su explicación, las anomalías sin revisar, los indicadores del
periodo reciente, el estado del modelo de predicción activo, y documentos
de resumen agregados (por categoría, por estado del catálogo y de
anomalías por severidad).

Los documentos de resumen existen para que una pregunta general ("¿cómo va
el inventario?") tenga algo mejor que responder que fichas de producto
sueltas: son los que
inventario/asistente/recuperador.py prioriza para ese tipo de pregunta.

Cada documento se guarda con su embedding (generado localmente, ver
embeddings.py) en DocumentoIndexado. El comando "indexar_conocimiento"
llama a reindexar() para regenerar el índice completo.
"""
from datetime import date, timedelta

from django.db.models import Count, Sum

from inventario.servicios.indicadores import (
    calcular_coi, calcular_ei, calcular_ns, hay_mezcla_de_origenes, rango_disponible,
)
from inventario.models import (
    Anomalia,
    Categoria,
    DocumentoIndexado,
    ModeloEntrenado,
    PedidoDetalle,
    Producto,
    Recomendacion,
    TipoDocumento,
)

from .embeddings import generar_embedding

# Ventana usada para "demanda reciente" en la ficha de producto. No se usa
# para el documento de indicadores: ese usa el mismo rango sin filtrar que
# el dashboard (ver _documento_indicadores), para que el mismo indicador no
# dé dos valores distintos según dónde se consulte.
DIAS_PERIODO_RECIENTE = 30


def _documentos_producto():
    hoy = date.today()
    desde = hoy - timedelta(days=DIAS_PERIODO_RECIENTE)
    documentos = []
    for producto in Producto.objects.filter(activo=True):
        demanda_reciente = PedidoDetalle.objects.filter(
            producto=producto,
            pedido__fecha_solicitud__date__gte=desde,
            pedido__fecha_solicitud__date__lte=hoy,
        ).aggregate(total=Sum('cantidad_solicitada'))['total'] or 0

        contenido = (
            f'Producto {producto.codigo} — {producto.nombre}. '
            f'Categoría: {producto.get_categoria_display()}. '
            f'Stock actual: {producto.stock_actual} unidades. '
            f'Precio de venta: S/ {producto.precio_venta}. '
            f'Demanda solicitada en los últimos {DIAS_PERIODO_RECIENTE} días: '
            f'{demanda_reciente} unidades.'
        )
        documentos.append((TipoDocumento.PRODUCTO, producto.pk, contenido))
    return documentos


# Estados que sí requieren que alguien haga algo. Los otros (normal,
# exceso) no se indexan uno por uno: quedan agrupados en un conteo dentro
# de _documento_resumen_estado, para que una pregunta por las
# recomendaciones no las liste todas repitiendo "no hace falta pedir".
ESTADOS_ACCIONABLES = (Recomendacion.Estado.CRITICO, Recomendacion.Estado.REPONER)


def _documentos_recomendacion():
    """Solo las del último lote generado (las "vigentes": un lote viejo ya
    no refleja el estado actual del inventario), y solo las que requieren
    una acción (crítico o para reponer). Las que no la requieren se cuentan
    agrupadas en _documento_resumen_estado en vez de indexarse una por una."""
    ultima_fecha = (
        Recomendacion.objects.order_by('-fecha_generacion').values_list('fecha_generacion', flat=True).first()
    )
    if ultima_fecha is None:
        return []
    documentos = []
    recomendaciones = Recomendacion.objects.filter(
        fecha_generacion=ultima_fecha, estado__in=ESTADOS_ACCIONABLES,
    ).select_related('producto')
    for recomendacion in recomendaciones:
        contenido = (
            f'Recomendación vigente para {recomendacion.producto.codigo} — '
            f'{recomendacion.producto.nombre} ({recomendacion.get_estado_display()}): pedir '
            f'{recomendacion.cantidad_sugerida:.0f} unidades. {recomendacion.explicacion}'
        )
        documentos.append((TipoDocumento.RECOMENDACION, recomendacion.pk, contenido))
    return documentos


def _documentos_anomalia():
    documentos = []
    for anomalia in Anomalia.objects.filter(revisada=False).select_related('producto'):
        # La descripción ya no nombra el producto (en pantalla va en su
        # propia columna); el documento del RAG sí lo necesita.
        contenido = (
            f'Anomalía sin revisar ({anomalia.get_severidad_display()}, '
            f'{anomalia.get_tipo_display()}) en {anomalia.producto.codigo} — '
            f'{anomalia.producto.nombre}: {anomalia.descripcion}'
        )
        documentos.append((TipoDocumento.ANOMALIA, anomalia.pk, contenido))
    return documentos


def _documento_indicadores():
    """Mismo cálculo, mismo rango de fechas y mismo origen (sin filtrar) que
    ve el dueño al abrir el dashboard sin tocar ningún filtro: si acá se
    usara una ventana distinta (p. ej. "últimos 30 días" fijo), el mismo
    indicador daría un número distinto según se consulte desde el dashboard
    o desde el asistente, lo cual no tiene sentido para un solo indicador.
    """
    fecha_minima, fecha_maxima = rango_disponible(origen=None)
    hoy = date.today()
    fecha_fin = fecha_maxima or hoy
    fecha_inicio = fecha_minima or fecha_fin

    ei = calcular_ei(fecha_inicio, fecha_fin)
    ns = calcular_ns(fecha_inicio, fecha_fin)
    coi = calcular_coi(fecha_inicio, fecha_fin)

    texto_ei = (
        f'Exactitud del inventario (EI): {ei["valor"]:.1f}%.' if ei['valor'] is not None
        else 'Exactitud del inventario (EI): sin conteos físicos en el periodo.'
    )
    texto_ns = (
        f'Nivel de servicio (NS): {ns["valor"]:.1f}%.' if ns['valor'] is not None
        else 'Nivel de servicio (NS): sin pedidos en el periodo.'
    )
    texto_coi = (
        f'Costos operativos de inventario (COI): S/ {coi["valor"]:.2f}.' if coi['tiene_datos']
        else 'Costos operativos de inventario (COI): sin datos en el periodo.'
    )
    texto_mezcla = (
        ' Advertencia: este cálculo mezcla datos de prueba y datos reales.'
        if hay_mezcla_de_origenes() else ''
    )

    contenido = (
        f'Indicadores del periodo {fecha_inicio:%d/%m/%Y} al {fecha_fin:%d/%m/%Y} '
        f'(el mismo rango completo que muestra el dashboard sin filtros aplicados): '
        f'{texto_ei} {texto_ns} {texto_coi}{texto_mezcla}'
    )
    return [(TipoDocumento.INDICADOR, None, contenido)]


def _documento_modelo():
    modelo = (
        ModeloEntrenado.objects.filter(fase=ModeloEntrenado.Fase.AJUSTADO, activo=True).first()
        or ModeloEntrenado.objects.filter(fase=ModeloEntrenado.Fase.BASE, activo=True).first()
    )
    if modelo is None:
        return []
    contenido = (
        f'Modelo de predicción de demanda activo: {modelo.get_fase_display()}, '
        f'entrenado el {modelo.fecha_entrenamiento:%d/%m/%Y}. '
        f'MAE: {modelo.mae:.2f}, RMSE: {modelo.rmse:.2f}, R²: {modelo.r2:.2f}.'
    )
    return [(TipoDocumento.MODELO, modelo.pk, contenido)]


def _documentos_resumen_categoria():
    """Un documento por categoría comercial con el total de productos
    activos y su stock agregado: la vista de conjunto que le falta a las
    fichas de producto sueltas para responder "¿cómo está el inventario de
    esmaltes?" sin traer productos al azar."""
    filas = (
        Producto.objects.filter(activo=True)
        .values('categoria')
        .annotate(total_productos=Count('id'), stock_total=Sum('stock_actual'))
        .order_by('-stock_total')
    )
    etiquetas = dict(Categoria.choices)
    documentos = []
    for fila in filas:
        etiqueta = etiquetas.get(fila['categoria'], fila['categoria'])
        contenido = (
            f'Resumen de la categoría {etiqueta}: {fila["total_productos"]} producto(s) activo(s), '
            f'stock total de {fila["stock_total"] or 0} unidades.'
        )
        documentos.append((TipoDocumento.RESUMEN_CATEGORIA, None, contenido))
    return documentos


def _documento_resumen_estado():
    """Conteo de productos por estado (crítico/reponer/normal/exceso) del
    último lote de recomendaciones generado: el resumen que responde
    "¿cómo va el negocio?" de un vistazo, en vez de una ficha de producto."""
    ultima_fecha = (
        Recomendacion.objects.order_by('-fecha_generacion').values_list('fecha_generacion', flat=True).first()
    )
    if ultima_fecha is None:
        return []

    conteos = {estado: 0 for estado in Recomendacion.Estado.values}
    filas = (
        Recomendacion.objects.filter(fecha_generacion=ultima_fecha)
        .values('estado').annotate(total=Count('id'))
    )
    for fila in filas:
        conteos[fila['estado']] = fila['total']
    total = sum(conteos.values())
    # Ya sumado acá para que el modelo de lenguaje no tenga que calcular
    # "normal + exceso" por su cuenta al responder cuántos productos no
    # requieren reposición (ver ESTADOS_ACCIONABLES en _documentos_recomendacion).
    sin_reposicion = conteos[Recomendacion.Estado.NORMAL] + conteos[Recomendacion.Estado.EXCESO]

    contenido = (
        f'Estado del catálogo según el último lote de recomendaciones ({ultima_fecha:%d/%m/%Y}), '
        f'de {total} producto(s) evaluado(s): '
        f'{conteos[Recomendacion.Estado.CRITICO]} en estado crítico, '
        f'{conteos[Recomendacion.Estado.REPONER]} para reponer, '
        f'{sin_reposicion} no requieren reposición '
        f'({conteos[Recomendacion.Estado.NORMAL]} en estado normal y '
        f'{conteos[Recomendacion.Estado.EXCESO]} en exceso de stock).'
    )
    return [(TipoDocumento.RESUMEN_ESTADO, None, contenido)]


def _documento_resumen_anomalias():
    """Conteo de anomalías sin revisar por severidad: siempre se genera,
    incluso en cero, para que el asistente pueda decir "no hay anomalías
    pendientes" en vez de no tener nada que decir al respecto."""
    conteos = {severidad: 0 for severidad in Anomalia.Severidad.values}
    filas = (
        Anomalia.objects.filter(revisada=False)
        .values('severidad').annotate(total=Count('id'))
    )
    for fila in filas:
        conteos[fila['severidad']] = fila['total']
    total = sum(conteos.values())

    if total == 0:
        contenido = 'Resumen de anomalías: no hay anomalías sin revisar.'
    else:
        contenido = (
            f'Resumen de anomalías sin revisar ({total} en total): '
            f'{conteos[Anomalia.Severidad.ALTA]} de severidad alta, '
            f'{conteos[Anomalia.Severidad.MEDIA]} de severidad media, '
            f'{conteos[Anomalia.Severidad.BAJA]} de severidad baja.'
        )
    return [(TipoDocumento.RESUMEN_ANOMALIAS, None, contenido)]


def construir_documentos():
    """Arma la lista completa de documentos (tipo, referencia_id,
    contenido) a indexar, sin generar todavía los embeddings."""
    documentos = []
    documentos += _documentos_producto()
    documentos += _documentos_recomendacion()
    documentos += _documentos_anomalia()
    documentos += _documento_indicadores()
    documentos += _documento_modelo()
    documentos += _documentos_resumen_categoria()
    documentos += _documento_resumen_estado()
    documentos += _documento_resumen_anomalias()
    return documentos


def reindexar():
    """Regenera el índice completo: borra los DocumentoIndexado existentes
    y crea uno nuevo por cada elemento de construir_documentos(), con su
    embedding. Devuelve la cantidad de documentos indexados."""
    documentos = construir_documentos()
    DocumentoIndexado.objects.all().delete()
    nuevos = [
        DocumentoIndexado(
            tipo=tipo, referencia_id=referencia_id, contenido=contenido,
            embedding=generar_embedding(contenido),
        )
        for tipo, referencia_id, contenido in documentos
    ]
    DocumentoIndexado.objects.bulk_create(nuevos)
    return len(nuevos)
