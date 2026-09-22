"""Modelos de la app, divididos por dominio:

- operacion.py: catálogo, compras, movimientos de stock, pedidos y conteos
  físicos (la operación diaria de la microempresa).
- costos.py: mermas y costos de almacenamiento (indicador COI).
- ia.py: motor de predicción de demanda, motor de decisiones, detección de
  anomalías y asistente conversacional.

Todo se reexporta aquí para que el resto del código siga usando
`from inventario.models import X` o `from .models import X` sin importar de
qué submódulo viene cada modelo.
"""
from .costos import CostoAlmacenamiento, Merma
from .ia import (
    Anomalia,
    ConsultaAsistente,
    DocumentoIndexado,
    ModeloEntrenado,
    NivelPrediccion,
    Prediccion,
    Recomendacion,
    TipoDocumento,
)
from .operacion import (
    Categoria,
    Compra,
    CompraDetalle,
    ConteoDetalle,
    ConteoFisico,
    Movimiento,
    Origen,
    Pedido,
    PedidoDetalle,
    Producto,
    Proveedor,
)

__all__ = [
    'Anomalia',
    'Categoria',
    'Compra',
    'CompraDetalle',
    'ConsultaAsistente',
    'ConteoDetalle',
    'ConteoFisico',
    'CostoAlmacenamiento',
    'DocumentoIndexado',
    'Merma',
    'ModeloEntrenado',
    'Movimiento',
    'NivelPrediccion',
    'Origen',
    'Pedido',
    'PedidoDetalle',
    'Prediccion',
    'Producto',
    'Proveedor',
    'Recomendacion',
    'TipoDocumento',
]
