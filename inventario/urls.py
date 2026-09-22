from django.urls import path

from .views import anomalias, asistente, conteos, dashboard, movimientos, pedidos, recomendaciones

app_name = 'inventario'

urlpatterns = [
    path('', dashboard.dashboard, name='dashboard'),

    path('pedidos/', pedidos.pedido_lista, name='pedido_lista'),
    path('pedidos/nuevo/', pedidos.pedido_nuevo, name='pedido_nuevo'),
    path(
        'pedidos/lineas/<int:detalle_id>/completar/',
        pedidos.pedido_completar_linea, name='pedido_completar_linea',
    ),
    path('pedidos/validar-linea/', pedidos.pedido_validar_linea, name='pedido_validar_linea'),

    path('movimientos/nuevo/', movimientos.movimiento_nuevo, name='movimiento_nuevo'),
    path('movimientos/validar/', movimientos.movimiento_validar, name='movimiento_validar'),

    path('conteos/nuevo/', conteos.conteo_nuevo, name='conteo_nuevo'),

    path('recomendaciones/', recomendaciones.recomendaciones_lista, name='recomendaciones_lista'),
    path(
        'recomendaciones/<int:pk>/decidir/',
        recomendaciones.recomendacion_decidir, name='recomendacion_decidir',
    ),

    path('anomalias/', anomalias.anomalias_lista, name='anomalias_lista'),
    path(
        'anomalias/<int:pk>/revisar/',
        anomalias.anomalia_marcar_revisada, name='anomalia_marcar_revisada',
    ),

    path('asistente/', asistente.asistente_chat, name='asistente_chat'),
    path('asistente/limpiar/', asistente.asistente_limpiar, name='asistente_limpiar'),
    path('asistente/stream/', asistente.asistente_stream, name='asistente_stream'),
]
