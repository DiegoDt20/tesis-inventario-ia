"""Comando de gestión: optimización de hiperparámetros y variantes del
motor de predicción de demanda (ver inventario/ml/optimizacion.py).

No activa ni registra ningún modelo: solo imprime la tabla comparativa y la
guarda como reporte (JSON y CSV) en artefactos/optimizacion/. El modelo
vigente sigue siendo el que esté activo en ModeloEntrenado.
"""
import csv
import json
from pathlib import Path

import numpy as np
import xgboost as xgb
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from inventario.ml.carga_externa import RUTA_POR_DEFECTO, leer_datos_externos
from inventario.ml.carga_interna import (
    MIN_DIAS_VENTA_CATEGORIA,
    leer_demanda_interna,
    leer_demanda_interna_por_categoria,
    seleccionar_categorias_aptas,
)
from inventario.ml.entrenamiento import MIN_DIAS_ENTRENAMIENTO
from inventario.ml.optimizacion import ESPACIO_AJUSTE, Optimizador
from inventario.models import ModeloEntrenado, NivelPrediccion, Origen

DIR_REPORTES = Path('artefactos/optimizacion')


class Command(BaseCommand):
    help = (
        'Busca hiperparámetros del ajuste con validación cruzada temporal, '
        'prueba variantes de la fase base y del conjunto de variables, y '
        'compara todo sobre el mismo test temporal. No activa ningún modelo.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--origen', default=Origen.REAL, choices=Origen.values,
            help='Origen de los pedidos internos (por defecto "real").',
        )
        parser.add_argument(
            '--nivel', default=NivelPrediccion.CATEGORIA, choices=NivelPrediccion.values,
            help='Granularidad, como en entrenar_ajustado (por defecto "categoria", la del modelo vigente).',
        )
        parser.add_argument(
            '--dias-test', type=int, default=7,
            help='Días finales de los datos internos usados como test temporal (por defecto 7).',
        )
        parser.add_argument(
            '--pliegues', type=int, default=4,
            help='Pliegues de la validación cruzada temporal sobre los días de entrenamiento (por defecto 4).',
        )
        parser.add_argument(
            '--iteraciones', type=int, default=100,
            help='Candidatos aleatorios de la búsqueda del ajuste, además del actual (por defecto 100).',
        )
        parser.add_argument(
            '--quitar', type=int, default=3,
            help='Cantidad de variables de menor importancia a quitar en esa variante (por defecto 3).',
        )
        parser.add_argument('--semilla', type=int, default=42, help='Semilla de la búsqueda (por defecto 42).')
        parser.add_argument(
            '--semillas-ruido', type=int, default=10,
            help=(
                'Repeticiones de la configuración actual con semillas distintas para '
                'estimar el ruido del R² (por defecto 10; 0 para omitirlo).'
            ),
        )
        parser.add_argument(
            '--archivo-externo', default=RUTA_POR_DEFECTO,
            help=f'CSV del dataset externo (por defecto {RUTA_POR_DEFECTO}).',
        )

    def handle(self, *args, **options):
        nivel = options['nivel']
        dias_test = options['dias_test']
        pliegues = options['pliegues']

        modelo_base = ModeloEntrenado.objects.filter(fase=ModeloEntrenado.Fase.BASE, activo=True).first()
        if modelo_base is None:
            raise CommandError('No hay un modelo base activo. Corre "entrenar_base" primero.')
        modelo_vigente = ModeloEntrenado.objects.filter(
            fase=ModeloEntrenado.Fase.AJUSTADO, activo=True, nivel=nivel,
        ).first()

        df_interno = self._cargar_interno(options['origen'], nivel)
        n_dias = (df_interno['fecha'].max() - df_interno['fecha'].min()).days + 1
        dias_entrenamiento = n_dias - dias_test
        if dias_entrenamiento < MIN_DIAS_ENTRENAMIENTO:
            raise CommandError(
                f'Los datos internos cubren {n_dias} día(s); con --dias-test {dias_test} '
                f'solo quedarían {dias_entrenamiento} día(s) para entrenar, menos de los '
                f'{MIN_DIAS_ENTRENAMIENTO} mínimos.'
            )
        if pliegues < 2 or dias_entrenamiento // (pliegues + 1) < 1:
            raise CommandError(
                f'Con {dias_entrenamiento} día(s) de entrenamiento no se pueden armar {pliegues} pliegues.'
            )

        try:
            df_externo = leer_datos_externos(options['archivo_externo'])
        except FileNotFoundError as exc:
            raise CommandError(f'No se encontró el archivo: {options["archivo_externo"]}') from exc

        base_actual = xgb.XGBRegressor()
        base_actual.load_model(modelo_base.ruta_archivo)

        self.stdout.write(
            f'Datos internos (origen={options["origen"]}, nivel={nivel}): {len(df_interno):,} registros, '
            f'{dias_entrenamiento} día(s) de entrenamiento y {dias_test} de prueba. '
            f'Validación cruzada temporal con {pliegues} pliegues sobre los días de entrenamiento.'
        )
        optimizador = Optimizador(
            df_interno, df_externo, dias_test,
            n_pliegues=pliegues, n_iteraciones=options['iteraciones'],
            n_quitar=options['quitar'], semilla=options['semilla'],
            modelo_base_actual=base_actual, registrar=self.stdout.write,
        )
        filas = optimizador.ejecutar()
        if options['semillas_ruido']:
            self.stdout.write(
                f'Ruido: configuración actual con {options["semillas_ruido"]} semillas distintas...'
            )
            optimizador.estimar_ruido_semilla(options['semillas_ruido'])

        self._imprimir_particiones(optimizador)
        self._imprimir_tabla(filas)
        self._imprimir_ruido(optimizador)
        self._imprimir_detalle(optimizador)
        rutas = self._guardar_reporte(optimizador, filas, options, dias_entrenamiento, modelo_base, modelo_vigente)

        self.stdout.write(f'\nReporte guardado en {rutas[0]} y {rutas[1]}.')
        vigente = f'#{modelo_vigente.pk}' if modelo_vigente else '(ninguno)'
        self.stdout.write(self.style.WARNING(
            f'No se activó ni registró ningún modelo; el ajustado vigente sigue siendo {vigente}.'
        ))

    def _cargar_interno(self, origen, nivel):
        if nivel == NivelPrediccion.CATEGORIA:
            df = leer_demanda_interna_por_categoria(origen=origen)
            if df.empty:
                raise CommandError('No hay pedidos registrados para optimizar.')
            aptas, _, _ = seleccionar_categorias_aptas(df, min_dias_venta=MIN_DIAS_VENTA_CATEGORIA)
            if not aptas:
                raise CommandError(f'Ninguna categoría llega a {MIN_DIAS_VENTA_CATEGORIA} días con venta.')
            self.stdout.write(f'Categorías aptas: {", ".join(aptas)}.')
            return df[df['serie_id'].isin(aptas)].reset_index(drop=True)
        df = leer_demanda_interna(origen=origen)
        if df.empty:
            raise CommandError('No hay pedidos registrados para optimizar.')
        return df

    def _imprimir_particiones(self, optimizador):
        self.stdout.write('\nPliegues de la validación cruzada (solo días de entrenamiento):')
        for i, (idx_ent, idx_val) in enumerate(optimizador.particiones, start=1):
            ent = optimizador.train.loc[idx_ent, 'fecha']
            val = optimizador.train.loc[idx_val, 'fecha']
            self.stdout.write(
                f'  {i}: entrena {ent.min():%d/%m}–{ent.max():%d/%m} ({ent.nunique()} días), '
                f'valida {val.min():%d/%m}–{val.max():%d/%m} ({val.nunique()} días)'
            )
        test = optimizador.test['fecha']
        self.stdout.write(
            f'  Test (no participa de ninguna elección): {test.min():%d/%m}–{test.max():%d/%m} '
            f'({test.nunique()} días, {len(optimizador.test)} filas)'
        )

    def _imprimir_tabla(self, filas):
        self.stdout.write('\nResultados sobre el mismo test, ordenados por R² en test:')
        encabezado = (
            f'{"#":>3}  {"Configuración":<60}{"R² CV":>8}{"MAE":>8}{"RMSE":>8}'
            f'{"SMAPE":>8}{"R²":>8}{"R² rec.":>9}'
        )
        self.stdout.write(encabezado)
        self.stdout.write('-' * len(encabezado))
        for i, fila in enumerate(filas, start=1):
            t = fila['test']
            nombre = f'[{fila["grupo"].split(".")[0]}] {fila["nombre"]}'
            self.stdout.write(
                f'{i:>3}  {nombre[:59]:<60}{fila["cv"]["r2"]:>8.3f}{t["mae"]:>8.2f}{t["rmse"]:>8.2f}'
                f'{t["smape"] * 100:>7.1f}%{t["r2"]:>8.4f}{fila["r2_recursivo"]:>9.4f}'
            )
        self.stdout.write(
            'R² CV: validación cruzada temporal fuera de pliegue (días de entrenamiento). '
            'R²: test a un paso. R² rec.: test pronosticado recursivamente desde el corte.'
        )

    def _imprimir_ruido(self, optimizador):
        if not optimizador.ruido_semilla:
            return
        self.stdout.write(
            f'\nRuido por semilla (configuración actual, {len(optimizador.ruido_semilla)} semillas; '
            'solo cambia el azar del submuestreo):'
        )
        for clave, etiqueta in (('r2_cv', 'R² CV'), ('r2', 'R² test'), ('r2_recursivo', 'R² rec.')):
            valores = np.array([c[clave] for c in optimizador.ruido_semilla])
            self.stdout.write(
                f'  {etiqueta:<9} media {valores.mean():.4f}  desv. {valores.std():.4f}  '
                f'rango {valores.min():.4f} a {valores.max():.4f}'
            )

    def _imprimir_detalle(self, optimizador):
        self.stdout.write('\nMejores candidatos de la búsqueda del ajuste (por R² CV):')
        for c in optimizador.candidatos_ajuste[:5]:
            params = {k: v for k, v in c['hiperparametros'].items() if k != 'random_state'}
            self.stdout.write(f'  R² CV {c["cv"]["r2"]:.4f}  {params}')
        self.stdout.write('\nImportancia de variables en el modelo ajustado actual (ganancia):')
        for variable, valor in optimizador.importancias:
            marca = '  ← quitada en la variante' if variable in optimizador.variables_quitadas else ''
            self.stdout.write(f'  {variable:<18}{valor:.4f}{marca}')

    def _guardar_reporte(self, optimizador, filas, options, dias_entrenamiento, modelo_base, modelo_vigente):
        DIR_REPORTES.mkdir(parents=True, exist_ok=True)
        sello = f'{options["nivel"]}_{timezone.now():%Y%m%d_%H%M%S}'
        ruta_json = DIR_REPORTES / f'optimizacion_{sello}.json'
        ruta_csv = DIR_REPORTES / f'optimizacion_{sello}.csv'

        filas_serializables = [{k: v for k, v in f.items() if k != 'modelo'} for f in filas]
        reporte = {
            'fecha': timezone.now().isoformat(),
            'origen': options['origen'],
            'nivel': options['nivel'],
            'dias_entrenamiento': dias_entrenamiento,
            'dias_prueba': options['dias_test'],
            'pliegues': options['pliegues'],
            'iteraciones': options['iteraciones'],
            'semilla': options['semilla'],
            'modelo_base_activo': modelo_base.pk,
            'modelo_ajustado_vigente': modelo_vigente.pk if modelo_vigente else None,
            'espacio_busqueda_ajuste': ESPACIO_AJUSTE,
            'importancia_variables': optimizador.importancias,
            'variables_quitadas': optimizador.variables_quitadas,
            'candidatos_ajuste': optimizador.candidatos_ajuste,
            'ruido_semilla': optimizador.ruido_semilla,
            'resultados': filas_serializables,
        }
        ruta_json.write_text(json.dumps(reporte, ensure_ascii=False, indent=2, default=str))

        with ruta_csv.open('w', newline='') as archivo:
            escritor = csv.writer(archivo)
            escritor.writerow([
                'posicion', 'grupo', 'configuracion', 'r2_cv', 'rmse_cv', 'mae', 'rmse', 'smape',
                'r2', 'r2_recursivo', 'hiperparametros_base', 'hiperparametros_ajuste', 'variables',
            ])
            for i, f in enumerate(filas_serializables, start=1):
                escritor.writerow([
                    i, f['grupo'], f['nombre'], f['cv']['r2'], f['cv']['rmse'], f['test']['mae'],
                    f['test']['rmse'], f['test']['smape'], f['test']['r2'], f['r2_recursivo'],
                    json.dumps(f.get('hiperparametros_base')), json.dumps(f.get('hiperparametros_ajuste')),
                    ' '.join(f.get('columnas', [])),
                ])
        return ruta_json, ruta_csv
