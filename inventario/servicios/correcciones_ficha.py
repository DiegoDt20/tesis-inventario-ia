"""Tabla de correcciones entre dos versiones de una ficha de registro.

Compara celda por celda la ficha anterior con la corregida y clasifica cada
diferencia, para el anexo de la tesis. La tabla sale de la comparación de
los archivos, no de una lista escrita a mano, así que se puede verificar
volviendo a correrla.

- Correcciones de registro: los campos de 2_NS que cambian los indicadores
  (código del producto, cantidad atendida y fecha de atención), una fila
  por registro y campo.
- Periodo: la fecha de corte de 1_EI y el periodo de 3_COI_CA, una fila por
  hoja aunque el cambio se repita en todas sus filas.
- Otras diferencias: todo lo demás que cambió entre los archivos (filas que
  aparecen o desaparecen, descripciones, "¿A tiempo?", Motivo, el bloque PD
  derivado de 2_NS), para que el anexo no omita ningún cambio.
"""
from dataclasses import dataclass
from datetime import date, datetime

import openpyxl

# (hoja, encabezado de columna) -> motivo, para las correcciones de registro.
CAMPOS_REGISTRO = {
    'Código del producto': 'Reasignación de códigos que identificaban dos productos distintos.',
    'Cant. atendida': (
        'Registro incompleto al momento del primer levantamiento, completado por la empresa.'
    ),
    'Fecha de atención': (
        'Registro incompleto al momento del primer levantamiento, completado por la empresa.'
    ),
}
MOTIVO_PERIODO = (
    'Alineación de la ventana de medición al mes calendario de agosto, para que los '
    'tres indicadores midan el mismo periodo.'
)


@dataclass
class Correccion:
    hoja: str
    registro: object
    campo: str
    anterior: object
    corregido: object
    motivo: str = ''


def _texto(valor):
    if isinstance(valor, datetime):
        valor = valor.date()
    if isinstance(valor, date):
        return valor.strftime('%d/%m/%Y')
    if valor is None:
        return '(vacío)'
    return str(valor)


def _tabla(hoja, marcador, clave=0):
    """{clave: {encabezado: valor}} de las filas de datos de la hoja. La
    fila de encabezado es la primera cuya columna A es `marcador`."""
    filas = list(hoja.iter_rows(values_only=True))
    inicio = next(i for i, f in enumerate(filas) if str(f[0] or '').strip().startswith(marcador))
    encabezados = [str(c).strip() if c is not None else None for c in filas[inicio]]
    datos = {}
    for fila in filas[inicio + 1:]:
        if fila[clave] is None or str(fila[0]).strip().upper().startswith('TOTAL'):
            continue
        datos[fila[clave]] = {e: v for e, v in zip(encabezados, fila) if e}
    return datos


def comparar_fichas(ruta_anterior, ruta_nueva):
    """Devuelve (registro, periodo, otras): tres listas de Correccion."""
    anterior = openpyxl.load_workbook(ruta_anterior, data_only=True)
    nueva = openpyxl.load_workbook(ruta_nueva, data_only=True)
    registro, periodo, otras = [], [], []

    def diferencias(nombre_hoja, marcador, clave=0, etiqueta_clave='N.°'):
        a = _tabla(anterior[nombre_hoja], marcador, clave)
        b = _tabla(nueva[nombre_hoja], marcador, clave)
        for k in sorted(set(a) | set(b), key=lambda x: (isinstance(x, str), x)):
            if k not in b:
                yield k, '(fila completa)', 'presente', 'eliminada', a[k]
            elif k not in a:
                yield k, '(fila completa)', 'ausente', 'agregada', b[k]
            else:
                for campo, valor in a[k].items():
                    if valor != b[k].get(campo):
                        yield k, campo, valor, b[k].get(campo), b[k]

    cortes = set()
    for k, campo, va, vb, _fila in diferencias('1_EI', 'N.'):
        if campo == 'Fecha de corte':
            cortes.add((_texto(va), _texto(vb)))
        else:
            otras.append(Correccion('1_EI', k, campo, va, vb))
    for va, vb in sorted(cortes):
        periodo.append(Correccion('1_EI', 'todas', 'Fecha de corte del conteo físico', va, vb, MOTIVO_PERIODO))

    for k, campo, va, vb, _fila in diferencias('2_NS', 'N.'):
        if campo in CAMPOS_REGISTRO:
            registro.append(Correccion('2_NS', k, campo, va, vb, CAMPOS_REGISTRO[campo]))
        else:
            otras.append(Correccion('2_NS', k, campo, va, vb))

    periodos_ca = set()
    for k, campo, va, vb, _fila in diferencias('3_COI_CA', 'Componente'):
        if campo == 'Periodo':
            periodos_ca.add((_texto(va), _texto(vb)))
        else:
            otras.append(Correccion('3_COI_CA', k, campo, va, vb))
    for va, vb in sorted(periodos_ca):
        periodo.append(Correccion('3_COI_CA', 'todas', 'Periodo de los costos de almacenamiento', va, vb, MOTIVO_PERIODO))

    # El bloque PD se empareja por N.° de pedido (columna B), no por su
    # propio N.°: al salir una línea, las siguientes se renumeran.
    for k, campo, va, vb, _fila in diferencias('3_COI_PD', 'N.', clave=1):
        if campo == 'N.°':
            continue
        otras.append(Correccion('3_COI_PD', f'pedido {k}', campo, va, vb))
    return registro, periodo, otras
