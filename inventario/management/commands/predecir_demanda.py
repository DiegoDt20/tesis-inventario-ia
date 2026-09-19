"""Comando de gestión: genera predicciones de demanda diaria para los
próximos N días, usando el modelo ajustado activo (o el base si aún no hay
un modelo ajustado con datos internos).

Si el modelo activo es de nivel "categoria", predice la demanda de cada
categoría apta y la desagrega a producto según la participación histórica
de cada uno (inventario/ml/prediccion.py), pero sigue guardando Prediccion a
nivel de producto para que el motor de decisiones no cambie.
"""
import pandas as pd
import xgboost as xgb
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from inventario.ml.carga_interna import (
    MIN_DIAS_VENTA_CATEGORIA,
    leer_demanda_interna,
    leer_demanda_interna_por_categoria,
    seleccionar_categorias_aptas,
)
from inventario.ml.features import COLUMNAS_FEATURES, construir_features
from inventario.ml.prediccion import calcular_participacion, desagregar_prediccion_categoria
from inventario.models import ModeloEntrenado, NivelPrediccion, Prediccion, Producto


class Command(BaseCommand):
    help = (
        'Genera predicciones de demanda diaria para los próximos N días, '
        'usando el modelo ajustado activo (o el base si aún no hay uno '
        'ajustado con datos internos). Si el modelo es de nivel "categoria", '
        'desagrega la predicción a producto según la participación histórica.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--dias', type=int, default=30, help='Días a predecir hacia adelante.')
        parser.add_argument('--origen', default=None, help='Filtra el historial de pedidos por origen.')

    def handle(self, *args, **options):
        dias = options['dias']
        origen = options['origen']

        registro_modelo = (
            ModeloEntrenado.objects.filter(fase=ModeloEntrenado.Fase.AJUSTADO, activo=True).first()
            or ModeloEntrenado.objects.filter(fase=ModeloEntrenado.Fase.BASE, activo=True).first()
        )
        if registro_modelo is None:
            raise CommandError('No hay ningún modelo entrenado activo. Corre "entrenar_base" primero.')
        if registro_modelo.fase == ModeloEntrenado.Fase.BASE:
            self.stdout.write(self.style.WARNING(
                'Todavía no hay un modelo ajustado con datos internos; se usará el modelo base.'
            ))

        modelo = xgb.XGBRegressor()
        modelo.load_model(registro_modelo.ruta_archivo)

        if registro_modelo.nivel == NivelPrediccion.CATEGORIA:
            creadas = self._predecir_por_categoria(modelo, registro_modelo, dias, origen)
        else:
            creadas = self._predecir_por_producto(modelo, registro_modelo, dias, origen)

        self.stdout.write(self.style.SUCCESS(
            f'Se generaron {creadas} predicciones ({dias} días) con el modelo '
            f'{registro_modelo.get_fase_display()} #{registro_modelo.pk} (nivel {registro_modelo.nivel}).'
        ))

    def _predecir_por_producto(self, modelo, registro_modelo, dias, origen):
        df_interno = leer_demanda_interna(origen=origen)
        if df_interno.empty:
            raise CommandError('No hay pedidos registrados; no hay historial desde el cual predecir.')

        productos_por_id = {
            p.pk: p for p in Producto.objects.filter(pk__in=df_interno['serie_id'].unique())
        }
        fecha_generacion = timezone.now()

        creadas = 0
        with transaction.atomic():
            for serie_id, historial in df_interno.groupby('serie_id'):
                producto = productos_por_id.get(serie_id)
                if producto is None:
                    continue
                for fecha_objetivo, valor in self._predecir_serie(historial, modelo, dias):
                    Prediccion.objects.create(
                        producto=producto,
                        fecha_generacion=fecha_generacion,
                        fecha_objetivo=fecha_objetivo,
                        demanda_predicha=valor,
                        modelo=registro_modelo,
                        nivel_prediccion=NivelPrediccion.PRODUCTO,
                        participacion_usada=None,
                    )
                    creadas += 1
        return creadas

    def _predecir_por_categoria(self, modelo, registro_modelo, dias, origen):
        df_categorias = leer_demanda_interna_por_categoria(origen=origen)
        if df_categorias.empty:
            raise CommandError('No hay pedidos registrados; no hay historial desde el cual predecir.')

        aptas, no_aptas, dias_por_categoria = seleccionar_categorias_aptas(
            df_categorias, min_dias_venta=MIN_DIAS_VENTA_CATEGORIA,
        )
        if not aptas:
            raise CommandError(
                f'Ninguna categoría llega a {MIN_DIAS_VENTA_CATEGORIA} días con venta; '
                'no se puede predecir a nivel de categoría.'
            )

        self.stdout.write(f'Categorías aptas para predecir: {", ".join(aptas)}.')
        if no_aptas:
            self.stdout.write(
                f'Categorías fuera del modelo (solo punto de reorden): {", ".join(no_aptas)}.'
            )

        participacion = calcular_participacion(origen=origen)
        productos_por_id = {p.pk: p for p in Producto.objects.all()}
        fecha_generacion = timezone.now()

        creadas = 0
        with transaction.atomic():
            for categoria in aptas:
                historial = df_categorias[df_categorias['serie_id'] == categoria]
                for fecha_objetivo, valor_categoria in self._predecir_serie(historial, modelo, dias):
                    reparto = desagregar_prediccion_categoria(categoria, valor_categoria, participacion)
                    for producto_id, valor_producto, participacion_usada in reparto:
                        producto = productos_por_id.get(producto_id)
                        if producto is None:
                            continue
                        Prediccion.objects.create(
                            producto=producto,
                            fecha_generacion=fecha_generacion,
                            fecha_objetivo=fecha_objetivo,
                            demanda_predicha=valor_producto,
                            modelo=registro_modelo,
                            nivel_prediccion=NivelPrediccion.CATEGORIA,
                            participacion_usada=participacion_usada,
                        )
                        creadas += 1
        return creadas

    def _predecir_serie(self, historial, modelo, dias):
        """Genera `dias` predicciones futuras de forma recursiva para una
        única serie (un producto o una categoría): cada predicción se
        agrega al historial como si fuera un dato real, para que la
        siguiente pueda calcular sus rezagos y medias móviles (en el futuro
        no hay ventas reales todavía)."""
        serie = historial[['fecha', 'serie_id', 'cantidad']].copy()
        serie_id = serie['serie_id'].iloc[0]
        ultima_fecha = serie['fecha'].max()
        resultados = []

        for i in range(1, dias + 1):
            fecha_objetivo = ultima_fecha + pd.Timedelta(days=i)
            fila_objetivo = pd.DataFrame([{
                'fecha': fecha_objetivo, 'serie_id': serie_id, 'cantidad': 0,
            }])
            features = construir_features(pd.concat([serie, fila_objetivo], ignore_index=True))
            fila_features = features[features['fecha'] == fecha_objetivo]

            prediccion = max(float(modelo.predict(fila_features[COLUMNAS_FEATURES])[0]), 0.0)
            resultados.append((fecha_objetivo.date(), prediccion))

            serie = pd.concat([serie, pd.DataFrame([{
                'fecha': fecha_objetivo, 'serie_id': serie_id, 'cantidad': prediccion,
            }])], ignore_index=True)

        return resultados
