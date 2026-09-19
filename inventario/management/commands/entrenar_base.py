"""Comando de gestión: fase 1 del motor de predicción de demanda."""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from inventario.ml.carga_externa import RUTA_POR_DEFECTO, leer_datos_externos
from inventario.ml.entrenamiento import entrenar_fase_base, guardar_modelo
from inventario.ml.evaluacion import calcular_metricas, predecir_con_modelo
from inventario.models import ModeloEntrenado


class Command(BaseCommand):
    help = (
        'Fase 1: preentrena el motor de predicción de demanda con el dataset '
        'externo de Kaggle (Store Item Demand Forecasting Challenge) y guarda '
        'el modelo base para que "entrenar_ajustado" continúe sobre él.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--archivo', default=RUTA_POR_DEFECTO,
            help=f'Ruta al CSV externo (por defecto: {RUTA_POR_DEFECTO}).',
        )
        parser.add_argument(
            '--dias-test', type=int, default=30,
            help='Días finales del dataset usados como test temporal (por defecto 30).',
        )

    def handle(self, *args, **options):
        archivo = options['archivo']
        dias_test = options['dias_test']

        self.stdout.write(f'Leyendo dataset externo desde "{archivo}"...')
        try:
            df_externo = leer_datos_externos(archivo)
        except FileNotFoundError as exc:
            raise CommandError(f'No se encontró el archivo: {archivo}') from exc

        self.stdout.write(f'Filas leídas: {len(df_externo):,}. Construyendo variables y entrenando...')
        modelo, train, test, hiperparametros = entrenar_fase_base(df_externo, dias_test=dias_test)

        y_predicho = predecir_con_modelo(modelo, test)
        metricas = calcular_metricas(test['cantidad'], y_predicho)

        with transaction.atomic():
            ModeloEntrenado.objects.filter(
                fase=ModeloEntrenado.Fase.BASE, activo=True
            ).update(activo=False)

            nombre_archivo = f'base_{timezone.now():%Y%m%d_%H%M%S}.json'
            ruta = guardar_modelo(modelo, nombre_archivo)

            registro = ModeloEntrenado.objects.create(
                fase=ModeloEntrenado.Fase.BASE,
                algoritmo='XGBRegressor',
                hiperparametros=hiperparametros,
                mae=metricas['mae'],
                rmse=metricas['rmse'],
                smape=metricas['smape'],
                r2=metricas['r2'],
                n_registros_externos=len(df_externo),
                n_registros_internos=0,
                ruta_archivo=ruta,
                activo=True,
            )

        self.stdout.write(self.style.SUCCESS(
            f'\nModelo base guardado en {ruta} (ModeloEntrenado #{registro.pk}).'
        ))
        self.stdout.write(f'  Filas de train: {len(train):,} | Filas de test: {len(test):,}')
        self.stdout.write(f'\nMétricas sobre el test externo (últimos {dias_test} días):')
        self.stdout.write(f'  MAE:  {metricas["mae"]:.4f}')
        self.stdout.write(f'  RMSE: {metricas["rmse"]:.4f}')
        self.stdout.write(f'  SMAPE: {metricas["smape"] * 100:.2f}%')
        self.stdout.write(f'  R²:   {metricas["r2"]:.4f}')
