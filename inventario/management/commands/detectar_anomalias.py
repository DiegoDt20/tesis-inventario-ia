"""Comando de gestión: corre los detectores de anomalías de control de
existencias (diferencias de inventario y movimientos atípicos) y guarda los
resultados como registros Anomalia."""
from django.core.management.base import BaseCommand
from django.db import transaction

from inventario.ml.anomalias import detectar_diferencias_inventario, detectar_movimientos_atipicos
from inventario.models import Anomalia, Movimiento, Producto


class Command(BaseCommand):
    help = (
        'Corre los detectores de anomalías de control de existencias '
        '(diferencias de inventario sobre ConteoDetalle y movimientos '
        'atípicos sobre Movimiento) y guarda los resultados como registros '
        'Anomalia, sin revisar.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--origen', default=None,
            help='Filtra los conteos y movimientos usados por origen (prueba/real).',
        )

    def handle(self, *args, **options):
        origen = options['origen']

        hallazgos_diferencias = detectar_diferencias_inventario(origen=origen)
        hallazgos_movimientos, sin_historico = detectar_movimientos_atipicos(origen=origen)
        hallazgos = hallazgos_diferencias + hallazgos_movimientos

        creadas, actualizadas = self._guardar(hallazgos)

        self.stdout.write(self.style.SUCCESS(f'\nAnomalías detectadas: {len(hallazgos)}'))
        self.stdout.write(f'  Nuevas: {creadas} | Ya existentes, recalculadas: {actualizadas}')
        self.stdout.write(f'  Diferencias de inventario: {len(hallazgos_diferencias)}')
        self.stdout.write(f'  Movimientos atípicos:      {len(hallazgos_movimientos)}')

        self.stdout.write('\nPor severidad:')
        for severidad, etiqueta in Anomalia.Severidad.choices:
            n = sum(1 for h in hallazgos if h.severidad == severidad)
            self.stdout.write(f'  {etiqueta}: {n}')

        if sin_historico:
            self.stdout.write(self.style.WARNING(
                f'\nProductos sin histórico suficiente para el z-score ({len(sin_historico)}):'
            ))
            productos_por_id = {
                p.pk: p for p in Producto.objects.filter(pk__in=[pid for pid, _, _ in sin_historico])
            }
            etiquetas_tipo = dict(Movimiento.Tipo.choices)
            for producto_id, tipo, n_movimientos in sin_historico:
                producto = productos_por_id.get(producto_id)
                nombre = f'{producto.codigo} — {producto.nombre}' if producto else f'producto #{producto_id}'
                self.stdout.write(
                    f'  {nombre} ({etiquetas_tipo[tipo]}): {n_movimientos} movimiento(s), se necesitan '
                    f'al menos 5.'
                )

        productos = Producto.objects.in_bulk({h.producto_id for h in hallazgos})
        for hallazgo in sorted(hallazgos, key=lambda h: h.score, reverse=True):
            producto = productos[hallazgo.producto_id]
            self.stdout.write(
                f'\n  [{hallazgo.severidad.upper()}] {producto.codigo} — {producto.nombre}: '
                f'{hallazgo.descripcion}'
            )

    @staticmethod
    def _guardar(hallazgos):
        """Crea las anomalías nuevas y actualiza las que ya existían para el
        mismo ConteoDetalle o Movimiento (severidad, score, valores y
        descripción), sin tocar su fecha de detección ni su estado de
        revisión. Así, volver a correr el comando (p. ej. tras cambiar los
        umbrales de severidad) no duplica anomalías ni pierde lo revisado.
        Devuelve (creadas, actualizadas)."""
        creadas = actualizadas = 0
        with transaction.atomic():
            for hallazgo in hallazgos:
                if hallazgo.conteo_detalle_id:
                    clave = {'tipo': hallazgo.tipo, 'conteo_detalle_id': hallazgo.conteo_detalle_id}
                else:
                    clave = {'tipo': hallazgo.tipo, 'movimiento_id': hallazgo.movimiento_id}
                valores = {
                    'severidad': hallazgo.severidad,
                    'score': hallazgo.score,
                    'valor_observado': hallazgo.valor_observado,
                    'valor_esperado': hallazgo.valor_esperado,
                    'descripcion': hallazgo.descripcion,
                }
                existentes = Anomalia.objects.filter(**clave)
                if existentes.update(**valores):
                    actualizadas += 1
                    continue
                Anomalia.objects.create(
                    producto_id=hallazgo.producto_id,
                    fecha_deteccion=hallazgo.fecha_deteccion,
                    **clave,
                    **valores,
                )
                creadas += 1
        return creadas, actualizadas
