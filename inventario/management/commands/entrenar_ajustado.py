"""Comando de gestión: fase 2 del motor de predicción de demanda."""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from inventario.ml.carga_interna import (
    MIN_DIAS_VENTA_CATEGORIA,
    leer_demanda_interna,
    leer_demanda_interna_por_categoria,
    seleccionar_categorias_aptas,
)
from inventario.ml.entrenamiento import entrenar_fase_ajuste, guardar_modelo
from inventario.ml.evaluacion import comparar_modelos
from inventario.models import ModeloEntrenado, NivelPrediccion, Origen

ETIQUETAS_MODELOS = {
    'linea_base': 'Línea base ingenua',
    'solo_interno': 'Solo datos internos',
    'ajustado': 'Preentrenado + ajuste',
}

# Con menos días de entrenamiento que esto, el ajuste (y sobre todo la
# comparación contra la línea base y contra "solo datos internos") deja de
# ser confiable: XGBoost prácticamente memoriza esos pocos días en vez de
# aprender un patrón. No es un número mágico exacto, pero por debajo de dos
# semanas no hay ni un ciclo semanal completo que aprender.
MIN_DIAS_ENTRENAMIENTO = 14


class Command(BaseCommand):
    help = (
        'Fase 2: continúa el entrenamiento del modelo base con los datos '
        'internos de la microempresa (transferencia de aprendizaje) y '
        'compara el resultado contra una línea base ingenua y un modelo '
        'entrenado solo con datos internos.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--origen', default=Origen.REAL, choices=Origen.values,
            help=(
                'Origen de los pedidos internos usados para el ajuste (prueba/real). '
                'Por defecto "real": los datos de prueba son solo para desarrollo y '
                'no deben mezclarse con el modelo que se evalúa en el postest.'
            ),
        )
        parser.add_argument(
            '--dias-test', type=int, default=30,
            help='Días finales de los datos internos usados como test temporal (por defecto 30).',
        )
        parser.add_argument(
            '--nivel', default=NivelPrediccion.PRODUCTO, choices=NivelPrediccion.values,
            help=(
                'Granularidad del entrenamiento: "producto" (por defecto) o '
                '"categoria", para cuando ningún producto individual tiene '
                'suficiente historial de ventas.'
            ),
        )

    def handle(self, *args, **options):
        origen = options['origen']
        dias_test = options['dias_test']
        nivel = options['nivel']

        modelo_base = ModeloEntrenado.objects.filter(
            fase=ModeloEntrenado.Fase.BASE, activo=True
        ).first()
        if modelo_base is None:
            raise CommandError('No hay un modelo base activo. Corre "entrenar_base" primero.')

        if nivel == NivelPrediccion.CATEGORIA:
            df_interno = self._cargar_por_categoria(origen)
        else:
            df_interno = leer_demanda_interna(origen=origen)
            if df_interno.empty:
                raise CommandError('No hay pedidos registrados para entrenar el ajuste.')

        n_dias = (df_interno['fecha'].max() - df_interno['fecha'].min()).days + 1
        dias_entrenamiento = n_dias - dias_test
        if dias_entrenamiento < MIN_DIAS_ENTRENAMIENTO:
            raise CommandError(
                f'Los datos internos cubren {n_dias} día(s); con --dias-test {dias_test} '
                f'solo quedarían {dias_entrenamiento} día(s) para entrenar, menos de los '
                f'{MIN_DIAS_ENTRENAMIENTO} mínimos. Usa un --dias-test más chico '
                f'(por ejemplo, {max(n_dias - MIN_DIAS_ENTRENAMIENTO, 1)}) o espera a tener '
                'más histórico interno.'
            )

        self.stdout.write(
            f'Registros internos (origen={origen}): {len(df_interno):,}. '
            f'{dias_entrenamiento} día(s) de entrenamiento, {dias_test} día(s) de prueba. '
            'Ajustando el modelo base...'
        )
        modelo, train, test, hiperparametros = entrenar_fase_ajuste(
            df_interno, modelo_base.ruta_archivo, dias_test=dias_test,
        )

        comparacion = comparar_modelos(train, test, modelo)

        with transaction.atomic():
            ModeloEntrenado.objects.filter(
                fase=ModeloEntrenado.Fase.AJUSTADO, activo=True, nivel=nivel,
            ).update(activo=False)

            nombre_archivo = f'ajustado_{nivel}_{timezone.now():%Y%m%d_%H%M%S}.json'
            ruta = guardar_modelo(modelo, nombre_archivo)

            registro = ModeloEntrenado.objects.create(
                fase=ModeloEntrenado.Fase.AJUSTADO,
                nivel=nivel,
                algoritmo='XGBRegressor',
                hiperparametros=hiperparametros,
                mae=comparacion['ajustado']['mae'],
                rmse=comparacion['ajustado']['rmse'],
                smape=comparacion['ajustado']['smape'],
                r2=comparacion['ajustado']['r2'],
                mae_linea_base=comparacion['linea_base']['mae'],
                mae_solo_interno=comparacion['solo_interno']['mae'],
                rmse_linea_base=comparacion['linea_base']['rmse'],
                rmse_solo_interno=comparacion['solo_interno']['rmse'],
                smape_linea_base=comparacion['linea_base']['smape'],
                smape_solo_interno=comparacion['solo_interno']['smape'],
                r2_linea_base=comparacion['linea_base']['r2'],
                r2_solo_interno=comparacion['solo_interno']['r2'],
                n_registros_externos=modelo_base.n_registros_externos,
                n_registros_internos=len(df_interno),
                origen_datos_internos=origen,
                dias_entrenamiento=dias_entrenamiento,
                dias_prueba=dias_test,
                ruta_archivo=ruta,
                activo=True,
            )

        self.stdout.write(self.style.SUCCESS(
            f'\nModelo ajustado (nivel {nivel}) guardado en {ruta} (ModeloEntrenado #{registro.pk}).'
        ))
        self.stdout.write(
            f'  Días de entrenamiento: {dias_entrenamiento} | Días de prueba: {dias_test}'
        )
        self.stdout.write(f'  Filas de train: {len(train):,} | Filas de test: {len(test):,}')
        self.stdout.write(
            f'  Filas de test con demanda real cero: {comparacion["pct_demanda_cero"]:.1f}%'
        )
        self._imprimir_comparacion(comparacion)

    def _cargar_por_categoria(self, origen):
        """Lee la demanda agrupada por categoría, decide qué categorías
        tienen histórico suficiente (>= MIN_DIAS_VENTA_CATEGORIA días con
        venta) y devuelve el DataFrame filtrado solo a esas. Las que no
        entran se reportan pero quedan fuera del modelo: se gestionan solo
        con el punto de reorden del motor de decisiones."""
        df_categorias = leer_demanda_interna_por_categoria(origen=origen)
        if df_categorias.empty:
            raise CommandError('No hay pedidos registrados para entrenar el ajuste por categoría.')

        aptas, no_aptas, dias_por_categoria = seleccionar_categorias_aptas(
            df_categorias, min_dias_venta=MIN_DIAS_VENTA_CATEGORIA,
        )
        if not aptas:
            raise CommandError(
                f'Ninguna categoría llega a {MIN_DIAS_VENTA_CATEGORIA} días con venta; '
                'no se puede entrenar a nivel de categoría.'
            )

        self.stdout.write(
            f'\nSelección de categorías aptas (>= {MIN_DIAS_VENTA_CATEGORIA} días con venta):'
        )
        self.stdout.write('  Entran al modelo:')
        for categoria in aptas:
            self.stdout.write(f'    {categoria:<12}{dias_por_categoria[categoria]} días con venta')
        if no_aptas:
            self.stdout.write('  Fuera del modelo (solo punto de reorden):')
            for categoria in no_aptas:
                self.stdout.write(f'    {categoria:<12}{dias_por_categoria[categoria]} días con venta')
        self.stdout.write('')

        return df_categorias[df_categorias['serie_id'].isin(aptas)].reset_index(drop=True)

    def _imprimir_comparacion(self, comparacion):
        self.stdout.write('\nComparación de los tres modelos sobre el mismo test interno:')
        encabezado = f'{"Modelo":<24}{"MAE":>10}{"RMSE":>10}{"SMAPE":>10}{"R2":>10}'
        self.stdout.write(encabezado)
        for clave, etiqueta in ETIQUETAS_MODELOS.items():
            m = comparacion[clave]
            fila = (
                f'{etiqueta:<24}{m["mae"]:>10.4f}{m["rmse"]:>10.4f}'
                f'{m["smape"] * 100:>9.2f}%{m["r2"]:>10.4f}'
            )
            self.stdout.write(fila)
