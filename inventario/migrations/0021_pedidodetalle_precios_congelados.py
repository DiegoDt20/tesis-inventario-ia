from django.db import migrations, models
from django.db.models import OuterRef, Subquery


def reconstruir_precios(apps, schema_editor):
    """Las líneas de pedido que ya existían no guardaron el precio ni el
    costo vigentes al registrarse. Se reconstruyen con los valores ACTUALES
    del producto (24/09/2026, después de cargar_precios) y se marcan con
    precios_reconstruidos=True para dejar constancia de que no son el valor
    histórico real."""
    PedidoDetalle = apps.get_model('inventario', 'PedidoDetalle')
    Producto = apps.get_model('inventario', 'Producto')
    producto = Producto.objects.filter(pk=OuterRef('producto_id'))
    PedidoDetalle.objects.filter(precio_venta_unitario__isnull=True).update(
        precio_venta_unitario=Subquery(producto.values('precio_venta')[:1]),
        costo_compra_unitario=Subquery(producto.values('costo_compra')[:1]),
        precios_reconstruidos=True,
    )


class Migration(migrations.Migration):

    dependencies = [
        ('inventario', '0020_recomendacion_datos_auditoria'),
    ]

    operations = [
        migrations.AddField(
            model_name='pedidodetalle',
            name='precio_venta_unitario',
            field=models.DecimalField(decimal_places=2, editable=False, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name='pedidodetalle',
            name='costo_compra_unitario',
            field=models.DecimalField(decimal_places=2, editable=False, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name='pedidodetalle',
            name='precios_reconstruidos',
            field=models.BooleanField(default=False, editable=False),
        ),
        migrations.RunPython(reconstruir_precios, migrations.RunPython.noop),
    ]
