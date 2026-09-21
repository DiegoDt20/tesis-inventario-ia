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

from inventario.indicadores import calcular_coi, calcular_ei, calcular_ns
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

# Ventana usada tanto para "demanda reciente" en la ficha de producto como
# para los indicadores del periodo: suficiente para dar contexto sin
# depender de todo el histórico.
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


def _documentos_recomendacion():
    """Solo las del último lote generado (las "vigentes"): un lote viejo ya
    no refleja el estado actual del inventario."""
    ultima_fecha = (
        Recomendacion.objects.order_by('-fecha_generacion').values_list('fecha_generacion', flat=True).first()
    )
    if ultima_fecha is None:
        return []
    documentos = []
    for recomendacion in Recomendacion.objects.filter(fecha_generacion=ultima_fecha).select_related('producto'):
        contenido = (
            f'Recomendación vigente para {recomendacion.producto.codigo} — '
            f'{recomendacion.producto.nombre} ({recomendacion.get_estado_display()}): '
            f'{recomendacion.explicacion}'
        )
        documentos.append((TipoDocumento.RECOMENDACION, recomendacion.pk, contenido))
    return documentos


def _documentos_anomalia():
    documentos = []
    for anomalia in Anomalia.objects.filter(revisada=False).select_related('producto'):
        contenido = (
            f'Anomalía sin revisar ({anomalia.get_severidad_display()}, '
            f'{anomalia.get_tipo_display()}): {anomalia.descripcion}'
        )
        documentos.append((TipoDocumento.ANOMALIA, anomalia.pk, contenido))
    return documentos


def _documento_indicadores():
    hoy = date.today()
    desde = hoy - timedelta(days=DIAS_PERIODO_RECIENTE)
    ei = calcular_ei(desde, hoy)
    ns = calcular_ns(desde, hoy)
    coi = calcular_coi(desde, hoy)

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

    contenido = (
        f'Indicadores del periodo {desde:%d/%m/%Y} al {hoy:%d/%m/%Y}: '
        f'{texto_ei} {texto_ns} {texto_coi}'
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

    contenido = (
        f'Estado del catálogo según el último lote de recomendaciones ({ultima_fecha:%d/%m/%Y}), '
        f'de {total} producto(s) evaluado(s): '
        f'{conteos[Recomendacion.Estado.CRITICO]} en estado crítico, '
        f'{conteos[Recomendacion.Estado.REPONER]} para reponer, '
        f'{conteos[Recomendacion.Estado.NORMAL]} en estado normal, '
        f'{conteos[Recomendacion.Estado.EXCESO]} en exceso de stock.'
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
