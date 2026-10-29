from django.core.management.base import BaseCommand
from django.db import transaction

from inventario.models import Movimiento, Origen, Producto


def recalcular_stock(productos, origen=None):
    """Recalcula stock_actual de los productos indicados a partir de su
    historial de movimientos, en orden cronológico. Con `origen`, solo
    cuentan los movimientos de ese origen: sin filtro, un ajuste de prueba
    posterior (p. ej. un conteo de prueba de setiembre) pisa el stock real.
    Devuelve [(codigo, stock_anterior, stock_nuevo), ...] de los que
    cambiaron."""
    cambios = []
    with transaction.atomic():
        for producto in productos.select_for_update().order_by('pk'):
            stock = 0
            movimientos = producto.movimientos.order_by('fecha', 'pk')
            if origen:
                movimientos = movimientos.filter(origen=origen)
            for movimiento in movimientos:
                if movimiento.tipo == Movimiento.Tipo.INGRESO:
                    stock += movimiento.cantidad
                elif movimiento.tipo == Movimiento.Tipo.SALIDA:
                    stock -= movimiento.cantidad
                elif movimiento.tipo == Movimiento.Tipo.AJUSTE:
                    stock = movimiento.cantidad

            if stock != producto.stock_actual:
                cambios.append((producto.codigo, producto.stock_actual, stock))
                producto.stock_actual = stock
                producto.save(update_fields=['stock_actual'])
    return cambios


class Command(BaseCommand):
    help = (
        'Recalcula stock_actual de todos los productos a partir de su historial '
        'de movimientos (ingresos, salidas y ajustes), por si quedó descuadrado.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--origen', choices=Origen.values,
            help=(
                'Solo cuenta los movimientos de este origen. Sin él se mezclan: '
                'un ajuste de prueba posterior pisa el stock real.'
            ),
        )

    def handle(self, *args, **options):
        cambios = recalcular_stock(Producto.objects.all(), origen=options.get('origen'))
        for codigo, antes, despues in cambios:
            self.stdout.write(f'  {codigo}: {antes} -> {despues}')

        self.stdout.write(self.style.SUCCESS(
            f'Stock recalculado. Productos actualizados: {len(cambios)}.'
        ))
