"""Comando de gestión: regenera el índice de conocimiento (DocumentoIndexado)
que alimenta el RAG del asistente conversacional."""
from django.core.management.base import BaseCommand

from inventario.asistente.indexador import reindexar


class Command(BaseCommand):
    help = (
        'Regenera el índice de conocimiento del asistente conversacional: '
        'borra los DocumentoIndexado existentes y los reconstruye a partir '
        'del estado actual de productos, recomendaciones, anomalías, '
        'indicadores y modelo de predicción activo.'
    )

    def handle(self, *args, **options):
        total = reindexar()
        self.stdout.write(self.style.SUCCESS(f'Índice regenerado: {total} documento(s) indexado(s).'))
