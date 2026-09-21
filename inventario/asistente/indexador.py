"""Construye los documentos que alimentan el índice RAG del asistente
conversacional a partir de la base de datos: ficha de cada producto activo
con su stock/categoría/precio/demanda reciente, las recomendaciones
vigentes con su explicación, las anomalías sin revisar, los indicadores del
periodo reciente y el estado del modelo de predicción activo.

Cada documento se guarda con su embedding (generado localmente, ver
embeddings.py) en DocumentoIndexado. El comando "indexar_conocimiento"
llama a reindexar() para regenerar el índice completo.
"""
from datetime import date, timedelta

from django.db.models import Sum

from inventario.indicadores import calcular_coi, calcular_ei, calcular_ns
from inventario.models import (
    Anomalia,
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


def construir_documentos():
    """Arma la lista completa de documentos (tipo, referencia_id,
    contenido) a indexar, sin generar todavía los embeddings."""
    documentos = []
    documentos += _documentos_producto()
    documentos += _documentos_recomendacion()
    documentos += _documentos_anomalia()
    documentos += _documento_indicadores()
    documentos += _documento_modelo()
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
