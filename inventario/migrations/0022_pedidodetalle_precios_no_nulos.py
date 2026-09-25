# Separada de 0021: en PostgreSQL no se puede alterar la tabla en la misma
# transacción en que se actualizaron sus filas (pending trigger events).
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventario', '0021_pedidodetalle_precios_congelados'),
    ]

    operations = [
        migrations.AlterField(
            model_name='pedidodetalle',
            name='precio_venta_unitario',
            field=models.DecimalField(decimal_places=2, editable=False, max_digits=12),
        ),
        migrations.AlterField(
            model_name='pedidodetalle',
            name='costo_compra_unitario',
            field=models.DecimalField(decimal_places=2, editable=False, max_digits=12),
        ),
    ]
