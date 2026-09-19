"""Comando de gestión: completa la demanda real de predicciones vencidas y
reporta las métricas del motor de predicción de demanda ya en producción."""
from django.core.management.base import BaseCommand
from django.db.models import Sum
from django.utils import timezone

from inventario.ml.evaluacion import calcular_metricas
from inventario.models import PedidoDetalle, Prediccion


class Command(BaseCommand):
    help = (
        'Completa demanda_real en las predicciones cuya fecha_objetivo ya '
        'pasó (a partir de PedidoDetalle.cantidad_solicitada) y calcula MAE, '
        'RMSE, SMAPE y R² sobre todas las predicciones ya evaluadas.'
    )

    def handle(self, *args, **options):
        hoy = timezone.localdate()
        pendientes = Prediccion.objects.filter(
            fecha_objetivo__lte=hoy, demanda_real__isnull=True,
        ).select_related('producto')

        actualizadas = 0
        for prediccion in pendientes:
            demanda_real = PedidoDetalle.objects.filter(
                producto=prediccion.producto,
                pedido__fecha_solicitud__date=prediccion.fecha_objetivo,
            ).aggregate(total=Sum('cantidad_solicitada'))['total'] or 0
            prediccion.demanda_real = demanda_real
            prediccion.save(update_fields=['demanda_real'])
            actualizadas += 1

        self.stdout.write(f'Predicciones completadas con demanda real: {actualizadas}')

        evaluables = Prediccion.objects.filter(demanda_real__isnull=False)
        total = evaluables.count()
        if total == 0:
            self.stdout.write(self.style.WARNING(
                'No hay predicciones con demanda real todavía; nada que evaluar.'
            ))
            return

        y_real = [p.demanda_real for p in evaluables]
        y_predicho = [p.demanda_predicha for p in evaluables]
        metricas = calcular_metricas(y_real, y_predicho)

        self.stdout.write(self.style.SUCCESS(f'\nMétricas sobre {total} predicciones evaluadas:'))
        self.stdout.write(f'  MAE:  {metricas["mae"]:.4f}')
        self.stdout.write(f'  RMSE: {metricas["rmse"]:.4f}')
        self.stdout.write(f'  SMAPE: {metricas["smape"] * 100:.2f}%')
        self.stdout.write(f'  R²:   {metricas["r2"]:.4f}')
