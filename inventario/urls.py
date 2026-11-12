from django.urls import path

from .views import (
    anomalias, asistente, busqueda, compras, conteos, dashboard, modelo, movimientos, pedidos, productos,
    proveedores, recomendaciones, reportes,
)

app_name = 'inventario'

urlpatterns = [
    path('', dashboard.dashboard, name='dashboard'),
    path(
        'grafico-prediccion/', dashboard.dashboard_grafico_prediccion, name='dashboard_grafico_prediccion',
    ),

    path('buscar/', busqueda.busqueda_global, name='busqueda_global'),

    path('pedidos/', pedidos.pedido_lista, name='pedido_lista'),
    path('pedidos/nuevo/', pedidos.pedido_nuevo, name='pedido_nuevo'),
    path(
        'pedidos/lineas/<int:detalle_id>/completar/',
        pedidos.pedido_completar_linea, name='pedido_completar_linea',
    ),
    path('pedidos/validar-linea/', pedidos.pedido_validar_linea, name='pedido_validar_linea'),

    path('movimientos/', movimientos.movimiento_lista, name='movimiento_lista'),
    path('movimientos/nuevo/', movimientos.movimiento_nuevo, name='movimiento_nuevo'),
    path('movimientos/validar/', movimientos.movimiento_validar, name='movimiento_validar'),
    path(
        'movimientos/contexto-producto/',
        movimientos.movimiento_contexto_producto, name='movimiento_contexto_producto',
    ),

    path('compras/', compras.compra_lista, name='compra_lista'),
    path('compras/nueva/', compras.compra_nueva, name='compra_nueva'),
    path('compras/<int:pk>/', compras.compra_detalle, name='compra_detalle'),
    path('compras/<int:pk>/recibir/', compras.compra_recibir, name='compra_recibir'),
    path('compras/<int:pk>/cerrar/', compras.compra_cerrar, name='compra_cerrar'),

    path('productos/', productos.producto_lista, name='producto_lista'),
    path('productos/nuevo/', productos.producto_nuevo, name='producto_nuevo'),
    path('productos/<int:pk>/editar/', productos.producto_editar, name='producto_editar'),

    path('proveedores/', proveedores.proveedor_lista, name='proveedor_lista'),
    path('proveedores/nuevo/', proveedores.proveedor_nuevo, name='proveedor_nuevo'),
    path('proveedores/<int:pk>/editar/', proveedores.proveedor_editar, name='proveedor_editar'),

    path('conteos/', conteos.conteo_lista, name='conteo_lista'),
    path('conteos/nuevo/', conteos.conteo_nuevo, name='conteo_nuevo'),

    path('recomendaciones/', recomendaciones.recomendaciones_lista, name='recomendaciones_lista'),
    path(
        'recomendaciones/<int:pk>/decidir/',
        recomendaciones.recomendacion_decidir, name='recomendacion_decidir',
    ),

    path('anomalias/', anomalias.anomalias_lista, name='anomalias_lista'),
    path('anomalias/revisar/', anomalias.anomalias_marcar_revisadas, name='anomalias_marcar_revisadas'),
    path(
        'anomalias/<int:pk>/revisar/',
        anomalias.anomalia_marcar_revisada, name='anomalia_marcar_revisada',
    ),

    path('modelo/', modelo.modelo_detalle, name='modelo_detalle'),

    path('reportes/', reportes.reportes, name='reportes'),
    path('reportes/comparacion/', reportes.comparacion, name='comparacion'),

    path('asistente/', asistente.asistente_chat, name='asistente_chat'),
    path('asistente/panel/', asistente.asistente_panel, name='asistente_panel'),
    path('asistente/limpiar/', asistente.asistente_limpiar, name='asistente_limpiar'),
    path('asistente/stream/', asistente.asistente_stream, name='asistente_stream'),
]
