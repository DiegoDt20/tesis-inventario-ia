from django.urls import path

from . import views

app_name = 'inventario'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),

    path('pedidos/', views.pedido_lista, name='pedido_lista'),
    path('pedidos/nuevo/', views.pedido_nuevo, name='pedido_nuevo'),
    path(
        'pedidos/lineas/<int:detalle_id>/completar/',
        views.pedido_completar_linea, name='pedido_completar_linea',
    ),

    path('movimientos/nuevo/', views.movimiento_nuevo, name='movimiento_nuevo'),

    path('conteos/nuevo/', views.conteo_nuevo, name='conteo_nuevo'),

    path('recomendaciones/', views.recomendaciones_lista, name='recomendaciones_lista'),
    path(
        'recomendaciones/<int:pk>/decidir/',
        views.recomendacion_decidir, name='recomendacion_decidir',
    ),

    path('anomalias/', views.anomalias_lista, name='anomalias_lista'),
    path(
        'anomalias/<int:pk>/revisar/',
        views.anomalia_marcar_revisada, name='anomalia_marcar_revisada',
    ),
]
