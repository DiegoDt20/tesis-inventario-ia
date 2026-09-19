from django.core.management.base import BaseCommand
from django.db import transaction

from inventario.models import Movimiento, Producto


class Command(BaseCommand):
    help = (
        'Recalcula stock_actual de todos los productos a partir de su historial '
        'de movimientos (ingresos, salidas y ajustes), por si quedó descuadrado.'
    )

    def handle(self, *args, **options):
        actualizados = 0
        with transaction.atomic():
            for producto in Producto.objects.select_for_update().order_by('pk'):
                stock = 0
                movimientos = producto.movimientos.order_by('fecha', 'pk')
                for movimiento in movimientos:
                    if movimiento.tipo == Movimiento.Tipo.INGRESO:
                        stock += movimiento.cantidad
                    elif movimiento.tipo == Movimiento.Tipo.SALIDA:
                        stock -= movimiento.cantidad
                    elif movimiento.tipo == Movimiento.Tipo.AJUSTE:
                        stock = movimiento.cantidad

                if stock != producto.stock_actual:
                    self.stdout.write(
                        f'  {producto.codigo}: {producto.stock_actual} -> {stock}'
                    )
                    producto.stock_actual = stock
                    producto.save(update_fields=['stock_actual'])
                    actualizados += 1

        self.stdout.write(self.style.SUCCESS(
            f'Stock recalculado. Productos actualizados: {actualizados}.'
        ))
