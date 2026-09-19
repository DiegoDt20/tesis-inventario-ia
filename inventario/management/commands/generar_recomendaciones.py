"""Comando de gestión: genera recomendaciones de reposición para todos los
productos activos, combinando demanda predicha e historial real."""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from inventario.decisiones.motor import calcular_desviaciones_demanda, calcular_recomendacion
from inventario.models import Producto, Recomendacion


class Command(BaseCommand):
    help = (
        'Genera una Recomendacion de reposición por cada producto activo con '
        'predicciones de demanda disponibles (estado, stock de seguridad, '
        'punto de reorden y cantidad sugerida).'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--nivel-servicio', type=float, default=0.95,
            help='Nivel de servicio objetivo, entre 0 y 1 (por defecto 0.95).',
        )
        parser.add_argument(
            '--dias-cobertura', type=int, default=30,
            help='Días de demanda a cubrir con la cantidad sugerida (por defecto 30).',
        )

    def handle(self, *args, **options):
        nivel_servicio = options['nivel_servicio']
        dias_cobertura = options['dias_cobertura']

        if not (0 < nivel_servicio < 1):
            raise CommandError('--nivel-servicio debe estar entre 0 y 1 (por ejemplo 0.95).')

        desviaciones = calcular_desviaciones_demanda()
        # Un único valor para todo el lote: así se puede recuperar "la
        # última corrida completa" agrupando por fecha_generacion exacta.
        fecha_generacion = timezone.now()

        creadas = 0
        sin_prediccion = 0
        conteo_estados = {estado: 0 for estado in Recomendacion.Estado.values}

        with transaction.atomic():
            for producto in Producto.objects.filter(activo=True):
                datos = calcular_recomendacion(
                    producto, nivel_servicio, dias_cobertura, desviaciones=desviaciones,
                )
                if datos is None:
                    sin_prediccion += 1
                    continue
                Recomendacion.objects.create(
                    producto=producto, fecha_generacion=fecha_generacion, **datos,
                )
                creadas += 1
                conteo_estados[datos['estado']] += 1

        self.stdout.write(self.style.SUCCESS(f'\nRecomendaciones creadas: {creadas}'))
        for estado, etiqueta in Recomendacion.Estado.choices:
            self.stdout.write(f'  {etiqueta}: {conteo_estados[estado]}')
        if sin_prediccion:
            self.stdout.write(self.style.WARNING(
                f'\nProductos activos sin predicciones de demanda (omitidos): {sin_prediccion}'
            ))
