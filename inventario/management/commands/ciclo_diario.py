"""Corre en orden los cuatro pasos que dejan al día la parte inteligente
del sistema: pronóstico de demanda, recomendaciones de reposición,
detección de anomalías e índice del asistente.

El orden importa: las recomendaciones usan el último lote de predicciones
y el asistente indexa las recomendaciones y anomalías vigentes. Todos los
pasos usan el mismo origen, para que el pronóstico y la desviación de la
demanda partan del mismo histórico. No entrena ni activa modelos."""
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from inventario.models import Origen


class Command(BaseCommand):
    help = (
        'Ejecuta en orden predecir_demanda, generar_recomendaciones, detectar_anomalias e '
        'indexar_conocimiento con el mismo origen. Si un paso falla, no corre los siguientes.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--origen', default=Origen.REAL, choices=Origen.values)
        parser.add_argument('--dias', type=int, default=30, help='Días a predecir hacia adelante (30).')
        parser.add_argument(
            '--sin-indexar', action='store_true',
            help='No regenera el índice del asistente (el paso más lento).',
        )

    def handle(self, *args, **opciones):
        origen = opciones['origen']
        pasos = [
            ('predecir_demanda', {'dias': opciones['dias'], 'origen': origen}),
            ('generar_recomendaciones', {'origen': origen}),
            ('detectar_anomalias', {'origen': origen}),
        ]
        if not opciones['sin_indexar']:
            pasos.append(('indexar_conocimiento', {}))
            if settings.ASISTENTE_ORIGEN != origen:
                self.stdout.write(self.style.WARNING(
                    f'El asistente indexa el origen "{settings.ASISTENTE_ORIGEN}" (ASISTENTE_ORIGEN), '
                    f'no "{origen}".'
                ))

        hechos = []
        for numero, (comando, argumentos) in enumerate(pasos, start=1):
            self.stdout.write(self.style.MIGRATE_HEADING(f'\n[{numero}/{len(pasos)}] {comando}'))
            try:
                call_command(comando, stdout=self.stdout, stderr=self.stderr, **argumentos)
            except Exception as error:
                pendientes = [nombre for nombre, _ in pasos[numero:]]
                raise CommandError(
                    f'Falló "{comando}": {error}. '
                    f'Completados: {", ".join(hechos) or "ninguno"}. '
                    f'Sin ejecutar: {", ".join(pendientes) or "ninguno"}.'
                ) from error
            hechos.append(comando)

        self.stdout.write(self.style.SUCCESS(f'\nCiclo completo ({len(hechos)} pasos, origen "{origen}").'))
