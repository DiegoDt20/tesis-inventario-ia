"""Comando de gestión: asigna o corrige el lead time (en días) de productos.

La importación (cargar_datos) deja lead_time_dias en 0 porque el Excel no
trae ese dato, y un lead time de 0 anula el cálculo del stock de seguridad
(SS = z * desviación * raíz(lead_time) se vuelve 0). Este comando permite
asignar un valor razonable en bloque o corregir productos puntuales.
"""
from django.core.management.base import BaseCommand, CommandError

from inventario.models import Producto


class Command(BaseCommand):
    help = (
        'Asigna un lead time (en días) a los productos. Sin --codigo, aplica '
        'el valor a todos los productos que todavía tengan lead_time_dias en '
        '0. Con --codigo, ajusta un producto puntual sin importar su valor '
        'actual.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dias', type=int, required=True,
            help='Lead time en días a asignar (debe ser mayor a 0).',
        )
        parser.add_argument(
            '--codigo', default=None,
            help='Código de un producto puntual a ajustar (por defecto corrige todos los que estén en 0).',
        )

    def handle(self, *args, **options):
        dias = options['dias']
        codigo = options['codigo']

        if dias <= 0:
            raise CommandError(
                '--dias debe ser mayor a 0: un lead time de 0 no tiene sentido '
                'físico y anula el cálculo del stock de seguridad.'
            )

        if codigo:
            try:
                producto = Producto.objects.get(codigo=codigo)
            except Producto.DoesNotExist as exc:
                raise CommandError(f'No existe un producto con código "{codigo}".') from exc

            anterior = producto.lead_time_dias
            producto.lead_time_dias = dias
            producto.save(update_fields=['lead_time_dias'])
            self.stdout.write(self.style.SUCCESS(
                f'{producto.codigo}: lead_time_dias {anterior} -> {dias}.'
            ))
            return

        actualizados = Producto.objects.filter(lead_time_dias=0).update(lead_time_dias=dias)
        self.stdout.write(self.style.SUCCESS(
            f'Productos con lead_time_dias=0 actualizados a {dias}: {actualizados}'
        ))
