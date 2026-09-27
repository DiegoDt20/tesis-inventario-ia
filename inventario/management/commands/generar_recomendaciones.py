"""Comando de gestión: genera recomendaciones de reposición para todos los
productos activos, combinando demanda predicha e historial real. Los
productos sin pronóstico en el lote vigente se recomiendan igual, solo con
punto de reorden y stock mínimo."""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from inventario.decisiones.motor import calcular_estadisticas_demanda, calcular_recomendacion
from inventario.models import Origen, Prediccion, Producto, Recomendacion


class Command(BaseCommand):
    help = (
        'Genera una Recomendacion de reposición por cada producto activo '
        '(estado, stock de seguridad, punto de reorden y cantidad sugerida): '
        'con la demanda pronosticada si el producto está en el último lote de '
        'predicciones, o solo con punto de reorden y stock mínimo si no.'
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
        parser.add_argument(
            '--origen', default=Origen.REAL, choices=Origen.values,
            help=(
                'Origen de los pedidos con que se calcula la desviación de la demanda '
                '(stock de seguridad). Por defecto "real": nunca se mezclan orígenes.'
            ),
        )

    def handle(self, *args, **options):
        nivel_servicio = options['nivel_servicio']
        dias_cobertura = options['dias_cobertura']

        if not (0 < nivel_servicio < 1):
            raise CommandError('--nivel-servicio debe estar entre 0 y 1 (por ejemplo 0.95).')

        origen = options['origen']
        estadisticas = calcular_estadisticas_demanda(origen)
        ultima_generacion = Prediccion.fecha_ultimo_lote()
        # Un único valor para todo el lote: así se puede recuperar "la
        # última corrida completa" agrupando por fecha_generacion exacta.
        fecha_generacion = timezone.now()

        creadas = 0
        conteo_estados = {estado: 0 for estado in Recomendacion.Estado.values}
        conteo_metodos = {metodo: 0 for metodo in Recomendacion.Metodo.values}
        cortes_pronostico = set()

        with transaction.atomic():
            for producto in Producto.objects.filter(activo=True):
                datos = calcular_recomendacion(
                    producto, nivel_servicio, dias_cobertura, estadisticas=estadisticas, origen=origen,
                    ultima_generacion=ultima_generacion,
                )
                Recomendacion.objects.create(
                    producto=producto, fecha_generacion=fecha_generacion, **datos,
                )
                creadas += 1
                conteo_estados[datos['estado']] += 1
                conteo_metodos[datos['metodo']] += 1
                if datos['metodo'] == Recomendacion.Metodo.PRONOSTICO:
                    cortes_pronostico.add(datos['fecha_corte_historico'])

        self.stdout.write(self.style.SUCCESS(
            f'\nRecomendaciones creadas: {creadas} (desviación de la demanda con pedidos de origen "{origen}")'
        ))
        for estado, etiqueta in Recomendacion.Estado.choices:
            self.stdout.write(f'  {etiqueta}: {conteo_estados[estado]}')
        self.stdout.write('Por método:')
        for metodo, etiqueta in Recomendacion.Metodo.choices:
            self.stdout.write(f'  {etiqueta}: {conteo_metodos[metodo]}')

        fecha_corte = estadisticas['fecha_corte']
        if fecha_corte:
            self.stdout.write(f'Fecha de corte del histórico (origen "{origen}"): {fecha_corte:%d/%m/%Y}')
        # El pronóstico debe partir del mismo histórico que la desviación: si
        # no, el lote de predicciones se generó con otro origen de pedidos.
        otros_cortes = sorted(c for c in cortes_pronostico if c != fecha_corte)
        if otros_cortes:
            self.stdout.write(self.style.WARNING(
                'El último lote de predicciones parte de otra fecha de corte ('
                + ', '.join(f'{c:%d/%m/%Y}' for c in otros_cortes)
                + f'): probablemente se generó sin "--origen {origen}". Regenera con '
                f'"predecir_demanda --origen {origen}".'
            ))
