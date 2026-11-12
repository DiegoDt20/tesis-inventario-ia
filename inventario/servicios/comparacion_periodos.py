"""Comparación de los tres indicadores entre dos periodos (el pretest y el
postest del diseño preexperimental) y su exportación a Excel.

No calcula nada por su cuenta: cada valor sale de calcular_ei, calcular_ns
y calcular_coi (servicios/indicadores.py), las mismas funciones que usan
el dashboard, reportes y el comando reporte_indicadores. Aquí solo se ponen
los dos periodos lado a lado, se resta y se arma el detalle línea por línea
para los anexos.
"""
from datetime import timedelta

from django.db.models.functions import TruncMonth
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from ..models import ConteoDetalle, ConteoFisico, CostoAlmacenamiento, Pedido
from .indicadores import (
    _pedidos_del_periodo,
    calcular_coi,
    calcular_ei,
    calcular_ns,
    lineas_desabastecimiento,
)

FORMATO_MONEDA = '#,##0.00'
FORMATO_FECHA = 'DD/MM/YYYY'


def rango_mes(mes):
    """(primer día, último día) del mes calendario de la fecha dada."""
    inicio = mes.replace(day=1)
    fin = (inicio.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return inicio, fin


def meses_con_datos(origen=None):
    """Meses calendario (como date del día 1, de más antiguo a más reciente)
    que tienen algún dato para los indicadores: un conteo físico, un pedido
    o un costo de almacenamiento."""
    conteos = ConteoFisico.objects.all()
    pedidos = Pedido.objects.all()
    costos = CostoAlmacenamiento.objects.all()
    if origen:
        conteos = conteos.filter(origen=origen)
        pedidos = pedidos.filter(origen=origen)
        costos = costos.filter(origen=origen)

    meses = {f.replace(day=1) for f in conteos.values_list('fecha_corte', flat=True).distinct()}
    meses |= {f.replace(day=1) for f in costos.values_list('periodo_mes', flat=True).distinct()}
    # TruncMonth trunca en la zona horaria activa (America/Lima): un pedido
    # de la noche del día 31 no pasa al mes siguiente por estar en UTC.
    meses |= {
        m.date() for m in
        pedidos.annotate(mes=TruncMonth('fecha_solicitud')).values_list('mes', flat=True).distinct()
    }
    return sorted(meses)


def _fila(clave, nombre, unidad, antes, despues, mejor_si_sube, detalle_antes='', detalle_despues='',
          secundaria=False):
    """Una fila de la tabla comparativa. diferencia = después − antes (en
    puntos porcentuales para EI/NS, en soles para el COI); variacion es esa
    diferencia relativa al valor de antes, en porcentaje. Ambas quedan en
    None si falta alguno de los dos valores, y la variación también si el
    valor de antes es 0. 'sube' y 'mejora' son None cuando no hay
    diferencia."""
    diferencia = variacion = sube = mejora = None
    if antes is not None and despues is not None:
        diferencia = despues - antes
        if antes != 0:
            variacion = float(diferencia) / abs(float(antes)) * 100
        if diferencia != 0:
            sube = diferencia > 0
            mejora = sube == mejor_si_sube
    return {
        'clave': clave,
        'nombre': nombre,
        'unidad': unidad,
        'antes': antes,
        'despues': despues,
        'detalle_antes': detalle_antes,
        'detalle_despues': detalle_despues,
        'diferencia': diferencia,
        'diferencia_abs': abs(diferencia) if diferencia is not None else None,
        'variacion': variacion,
        'variacion_abs': abs(variacion) if variacion is not None else None,
        'sube': sube,
        'mejora': mejora,
        'secundaria': secundaria,
    }


def _medir(inicio, fin, origen):
    return {
        'inicio': inicio,
        'fin': fin,
        'ei': calcular_ei(inicio, fin, origen=origen),
        'ns': calcular_ns(inicio, fin, origen=origen),
        'coi': calcular_coi(inicio, fin, origen=origen),
    }


def _monto(coi, campo):
    """Un monto del COI, o None si el periodo no tuvo nada que medir (0.00
    se leería como "no hubo costos")."""
    return coi[campo] if coi['tiene_datos'] else None


def comparar_periodos(antes, despues, origen=None):
    """Compara los indicadores de dos periodos. antes y despues son tuplas
    (fecha_inicio, fecha_fin). Devuelve {'origen', 'antes', 'despues',
    'filas'}: 'antes' y 'despues' traen las fechas y el resultado completo
    de cada indicador; 'filas' es la tabla comparativa (EI, NS, COI y, como
    filas secundarias, los dos componentes del COI)."""
    a = _medir(*antes, origen)
    d = _medir(*despues, origen)

    def conteo(medida, clave_parcial):
        return f"{medida[clave_parcial]} de {medida['total']}" if medida['total'] else ''

    def lineas(coi):
        return f"{coi['lineas_desabastecimiento']} líneas, {coi['unidades_no_atendidas']} u." \
            if coi['tiene_datos'] else ''

    filas = [
        _fila('ei', 'Exactitud del inventario (EI)', '%', a['ei']['valor'], d['ei']['valor'], True,
              conteo(a['ei'], 'correctos'), conteo(d['ei'], 'correctos')),
        _fila('ns', 'Nivel de servicio (NS)', '%', a['ns']['valor'], d['ns']['valor'], True,
              conteo(a['ns'], 'a_tiempo'), conteo(d['ns'], 'a_tiempo')),
        _fila('coi', 'Costos operativos de inventario (COI)', 'S/',
              _monto(a['coi'], 'valor'), _monto(d['coi'], 'valor'), False),
        _fila('ca', 'Costos de almacenamiento (CA)', 'S/',
              _monto(a['coi'], 'almacenamiento'), _monto(d['coi'], 'almacenamiento'), False,
              secundaria=True),
        _fila('pd', 'Pérdidas por desabastecimiento (PD)', 'S/',
              _monto(a['coi'], 'desabastecimiento'), _monto(d['coi'], 'desabastecimiento'), False,
              lineas(a['coi']), lineas(d['coi']), secundaria=True),
    ]
    return {'origen': origen, 'antes': a, 'despues': d, 'filas': filas}


# ---------------------------------------------------------------------------
# Exportación a Excel
# ---------------------------------------------------------------------------

def _etiqueta_periodo(medida):
    return f"{medida['inicio']:%d/%m/%Y} – {medida['fin']:%d/%m/%Y}"


def _hoja(libro, titulo, encabezados, filas, formatos=None):
    """Agrega una hoja con encabezado en negrita, filas y formato de número
    por columna (formatos: {índice de columna desde 1: formato})."""
    hoja = libro.create_sheet(titulo)
    hoja.append(encabezados)
    for celda in hoja[1]:
        celda.font = Font(bold=True)
        celda.fill = PatternFill('solid', start_color='E7E9EC')
        celda.alignment = Alignment(wrap_text=True, vertical='center')
    for fila in filas:
        hoja.append(fila)
    for columna, formato in (formatos or {}).items():
        for (celda,) in hoja.iter_rows(min_row=2, min_col=columna, max_col=columna):
            celda.number_format = formato
    for indice, encabezado in enumerate(encabezados, start=1):
        largo = max([len(str(encabezado))] + [len(str(f[indice - 1])) for f in filas if f[indice - 1] is not None])
        hoja.column_dimensions[get_column_letter(indice)].width = min(max(largo + 2, 10), 60)
    hoja.freeze_panes = 'A2'
    return hoja


def _filas_ei(etiqueta, medida, origen):
    qs = ConteoDetalle.objects.filter(
        conteo__fecha_corte__gte=medida['inicio'], conteo__fecha_corte__lte=medida['fin'],
    )
    if origen:
        qs = qs.filter(conteo__origen=origen)
    qs = qs.select_related('conteo', 'producto').order_by('conteo__fecha_corte', 'producto__codigo')
    return [
        [etiqueta, d.conteo.fecha_corte, d.producto.codigo, d.producto.nombre, d.stock_sistema,
         d.stock_fisico, d.stock_fisico - d.stock_sistema, 'Sí' if d.stock_sistema == d.stock_fisico else 'No']
        for d in qs
    ]


def _fecha_local(momento):
    """Fecha (sin hora) de un datetime en la zona horaria activa; openpyxl
    no acepta datetimes con zona horaria."""
    return timezone.localtime(momento).date() if momento else None


def _filas_ns(etiqueta, medida, origen):
    qs = _pedidos_del_periodo(medida['inicio'], medida['fin'], origen).select_related(
        'pedido', 'producto',
    ).order_by('pedido__fecha_solicitud', 'pk')
    return [
        [etiqueta, _fecha_local(d.pedido.fecha_solicitud), d.producto.codigo, d.producto.nombre,
         d.cantidad_solicitada, d.cantidad_atendida, d.fecha_requerida, _fecha_local(d.fecha_atencion),
         'Sí' if d.atendido_a_tiempo else 'No', d.get_motivo_no_atencion_display() or '']
        for d in qs
    ]


def _filas_pd(etiqueta, medida, origen):
    qs = lineas_desabastecimiento(medida['inicio'], medida['fin'], origen).select_related(
        'pedido', 'producto',
    ).order_by('pedido__fecha_solicitud', 'pk')
    filas = []
    for d in qs:
        no_atendidas = d.cantidad_solicitada - d.cantidad_atendida
        margen = d.precio_venta_unitario - d.costo_compra_unitario
        filas.append([
            etiqueta, _fecha_local(d.pedido.fecha_solicitud), d.producto.codigo, d.producto.nombre,
            d.cantidad_solicitada, d.cantidad_atendida, no_atendidas, d.precio_venta_unitario,
            d.costo_compra_unitario, margen, no_atendidas * margen,
            'Sí' if d.precios_reconstruidos else 'No',
        ])
    return filas


def _filas_ca(etiqueta, medida):
    return [
        [etiqueta, c['periodo_mes'], c['concepto'], c['monto']]
        for c in medida['coi']['conceptos_almacenamiento']
    ]


def libro_comparacion(comparacion, etiqueta_antes='Antes', etiqueta_despues='Después'):
    """Libro de Excel con la tabla comparativa ("Resumen") y el detalle de
    cada indicador en ambos periodos (EI por producto contado, NS por línea
    de pedido, CA por concepto y PD por línea no atendida), para los anexos.
    Los valores son los mismos de comparar_periodos; las hojas de detalle
    listan los registros de los que salen. No incluye el nombre del
    cliente."""
    origen = comparacion['origen']
    antes, despues = comparacion['antes'], comparacion['despues']
    periodos = ((etiqueta_antes, antes), (etiqueta_despues, despues))

    libro = Workbook()
    libro.remove(libro.active)

    filas_resumen = []
    for f in comparacion['filas']:
        es_porcentaje = f['unidad'] == '%'

        def valor(v):
            if v is None:
                return None
            # Los porcentajes se redondean a 2 decimales (como en pantalla);
            # los montos ya son Decimal con 2 decimales.
            return round(v, 2) if es_porcentaje else v

        filas_resumen.append([
            f['nombre'], 'porcentaje' if es_porcentaje else 'soles',
            valor(f['antes']), f['detalle_antes'], valor(f['despues']), f['detalle_despues'],
            valor(f['diferencia']), round(f['variacion'], 2) if f['variacion'] is not None else None,
            {True: 'Mejora', False: 'Empeora'}.get(f['mejora'], 'Sin cambio' if f['diferencia'] == 0 else ''),
        ])
    hoja = _hoja(
        libro, 'Resumen',
        ['Indicador', 'Unidad', f'{etiqueta_antes} ({_etiqueta_periodo(antes)})', f'Detalle {etiqueta_antes.lower()}',
         f'{etiqueta_despues} ({_etiqueta_periodo(despues)})', f'Detalle {etiqueta_despues.lower()}',
         'Diferencia (después − antes)', 'Variación relativa (%)', 'Resultado'],
        filas_resumen, {3: FORMATO_MONEDA, 5: FORMATO_MONEDA, 7: FORMATO_MONEDA, 8: FORMATO_MONEDA},
    )
    hoja.append([])
    hoja.append(['La diferencia de EI y NS está en puntos porcentuales; la del COI, en soles.'])
    hoja.append([f"Origen de los datos: {origen or 'todos'}. Generado el {timezone.localtime():%d/%m/%Y %H:%M}."])

    _hoja(
        libro, 'EI por producto',
        ['Periodo', 'Fecha de corte', 'Código', 'Producto', 'Stock sistema', 'Stock físico', 'Diferencia',
         'Registro correcto'],
        [fila for etiqueta, medida in periodos for fila in _filas_ei(etiqueta, medida, origen)],
        {2: FORMATO_FECHA},
    )
    _hoja(
        libro, 'NS por línea',
        ['Periodo', 'Fecha del pedido', 'Código', 'Producto', 'Cantidad solicitada', 'Cantidad atendida',
         'Fecha requerida', 'Fecha de atención', 'Atendido a tiempo', 'Motivo de no atención'],
        [fila for etiqueta, medida in periodos for fila in _filas_ns(etiqueta, medida, origen)],
        {2: FORMATO_FECHA, 7: FORMATO_FECHA, 8: FORMATO_FECHA},
    )
    _hoja(
        libro, 'COI - CA',
        ['Periodo', 'Mes', 'Concepto', 'Monto (S/)'],
        [fila for etiqueta, medida in periodos for fila in _filas_ca(etiqueta, medida)],
        {2: 'MM/YYYY', 4: FORMATO_MONEDA},
    )
    _hoja(
        libro, 'COI - PD',
        ['Periodo', 'Fecha del pedido', 'Código', 'Producto', 'Cantidad solicitada', 'Cantidad atendida',
         'Unidades no atendidas', 'Precio de venta (S/)', 'Costo de compra (S/)', 'Margen unitario (S/)',
         'Pérdida (S/)', 'Precio reconstruido'],
        [fila for etiqueta, medida in periodos for fila in _filas_pd(etiqueta, medida, origen)],
        {2: FORMATO_FECHA, 8: FORMATO_MONEDA, 9: FORMATO_MONEDA, 10: FORMATO_MONEDA, 11: FORMATO_MONEDA},
    )
    return libro

