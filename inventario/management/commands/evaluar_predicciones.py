"""Comando de gestión: completa la demanda real de predicciones vencidas y
reporta las métricas del motor de predicción de demanda ya en producción.

Además evalúa la exactitud del pronóstico del modelo ajustado vigente sobre
su propio test temporal (los mismos días de prueba con los que se entrenó,
ModeloEntrenado.dias_prueba) contra tres líneas base (ver
inventario/ml/evaluacion.py). No activa ni registra ningún modelo: solo
guarda los resultados de la corrida en el modelo vigente y en un JSON en
artefactos/evaluacion/.
"""
import json
from pathlib import Path

import xgboost as xgb
from django.core.management.base import BaseCommand
from django.db.models import Max, Sum
from django.utils import timezone

from inventario.ml.carga_interna import (
    MIN_DIAS_VENTA_CATEGORIA,
    leer_demanda_interna_por_categoria,
    seleccionar_categorias_aptas,
)
from inventario.ml.evaluacion import LINEAS_BASE, calcular_metricas, comparar_exactitud, predecir_con_modelo
from inventario.ml.features import construir_features, dividir_temporal
from inventario.models import ModeloEntrenado, NivelPrediccion, Origen, Pedido, PedidoDetalle, Prediccion

DIR_REPORTES = Path('artefactos/evaluacion')


def _pct(valor, decimales=1):
    """Fracción como porcentaje, o '—' si no está definida."""
    return '—' if valor is None else f'{valor * 100:.{decimales}f}%'


def _num(valor, decimales=4):
    return '—' if valor is None else f'{valor:.{decimales}f}'


class Command(BaseCommand):
    help = (
        'Completa demanda_real en las predicciones cuya fecha_objetivo ya '
        'pasó (a partir de PedidoDetalle.cantidad_solicitada) y calcula MAE, '
        'RMSE, SMAPE y R² sobre todas las predicciones ya evaluadas. Además '
        'evalúa la exactitud (WAPE, acierto dentro de tolerancia, R² por '
        'categoría) del modelo ajustado vigente contra tres líneas base.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--origen', default=Origen.REAL, choices=Origen.values,
            help='Origen de los pedidos con que se mide la demanda real (por defecto "real").',
        )

    def handle(self, *args, **options):
        origen = options['origen']
        self._completar_demanda_real(origen)
        self._metricas_predicciones()
        self._evaluar_exactitud(origen)

    # --- Predicciones en producción -----------------------------------------

    def _completar_demanda_real(self, origen):
        """Llena demanda_real con los pedidos del `origen` dado, solo hasta el
        último día con pedidos de ese origen: más allá todavía no hay datos
        cargados, y completar con cero confundiría "sin datos" con "sin
        demanda"."""
        ultimo_dia = Pedido.objects.filter(origen=origen).aggregate(
            ultima=Max('fecha_solicitud'),
        )['ultima']
        hoy = timezone.localdate()
        limite = min(hoy, timezone.localtime(ultimo_dia).date()) if ultimo_dia else None

        actualizadas = 0
        if limite is not None:
            pendientes = Prediccion.objects.filter(
                fecha_objetivo__lte=limite, demanda_real__isnull=True,
            ).select_related('producto')
            for prediccion in pendientes:
                demanda_real = PedidoDetalle.objects.filter(
                    producto=prediccion.producto,
                    pedido__origen=origen,
                    pedido__fecha_solicitud__date=prediccion.fecha_objetivo,
                ).aggregate(total=Sum('cantidad_solicitada'))['total'] or 0
                prediccion.demanda_real = demanda_real
                prediccion.save(update_fields=['demanda_real'])
                actualizadas += 1

        self.stdout.write(f'Predicciones completadas con demanda real: {actualizadas}')
        sin_datos = Prediccion.objects.filter(fecha_objetivo__lte=hoy, demanda_real__isnull=True).count()
        if sin_datos:
            hasta = f'{limite:%d/%m/%Y}' if limite else 'ninguna fecha'
            self.stdout.write(
                f'  {sin_datos} predicción(es) vencida(s) siguen sin demanda real: no hay pedidos '
                f'de origen "{origen}" cargados para esas fechas (hay hasta {hasta}).'
            )

    def _metricas_predicciones(self):
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

    # --- Exactitud del pronóstico sobre el test temporal --------------------

    def _evaluar_exactitud(self, origen):
        self.stdout.write(self.style.SUCCESS('\nExactitud del pronóstico (test temporal del modelo vigente)'))
        modelo = ModeloEntrenado.objects.filter(
            fase=ModeloEntrenado.Fase.AJUSTADO, activo=True, nivel=NivelPrediccion.CATEGORIA,
        ).first()
        if modelo is None:
            self.stdout.write(self.style.WARNING(
                '  No hay un modelo ajustado activo a nivel de categoría; se omite.'
            ))
            return
        if not modelo.dias_prueba:
            self.stdout.write(self.style.WARNING(
                f'  El modelo #{modelo.pk} no registra sus días de prueba; no se puede '
                'reconstruir su test temporal. Se omite.'
            ))
            return
        if modelo.origen_datos_internos and modelo.origen_datos_internos != origen:
            self.stdout.write(self.style.WARNING(
                f'  Ojo: el modelo #{modelo.pk} se ajustó con datos de origen '
                f'"{modelo.origen_datos_internos}" y se evalúa con "{origen}".'
            ))

        df = leer_demanda_interna_por_categoria(origen=origen)
        if df.empty:
            self.stdout.write(self.style.WARNING(f'  No hay pedidos de origen "{origen}"; se omite.'))
            return
        # Mismas categorías que entrenar_ajustado: el modelo solo pronostica
        # las aptas; las demás se gestionan con punto de reorden.
        aptas, no_aptas, dias_venta = seleccionar_categorias_aptas(df, min_dias_venta=MIN_DIAS_VENTA_CATEGORIA)
        df = df[df['serie_id'].isin(aptas)].reset_index(drop=True)

        dias_test = modelo.dias_prueba
        n_dias = (df['fecha'].max() - df['fecha'].min()).days + 1
        dias_entrenamiento = n_dias - dias_test
        train, test = dividir_temporal(construir_features(df), dias_test=dias_test)

        self.stdout.write(
            f'  Modelo #{modelo.pk} ({modelo.ruta_archivo}), origen={origen}. '
            f'Entrenamiento {train["fecha"].min():%d/%m}–{train["fecha"].max():%d/%m} '
            f'({dias_entrenamiento} días), prueba {test["fecha"].min():%d/%m}–{test["fecha"].max():%d/%m} '
            f'({dias_test} días, {len(test)} filas). Pronóstico a un paso.'
        )
        if modelo.dias_entrenamiento is not None and modelo.dias_entrenamiento != dias_entrenamiento:
            self.stdout.write(self.style.WARNING(
                f'  Ojo: el modelo se entrenó con {modelo.dias_entrenamiento} días y hoy los datos '
                f'dan {dias_entrenamiento}: el histórico cambió desde el entrenamiento.'
            ))
        self.stdout.write(f'  Categorías evaluadas: {", ".join(aptas)}.')
        if no_aptas:
            fuera = ', '.join(f'{c} ({dias_venta[c]} días con venta)' for c in no_aptas)
            self.stdout.write(
                f'  Sin pronóstico del modelo (menos de {MIN_DIAS_VENTA_CATEGORIA} días con venta; '
                f'solo punto de reorden): {fuera}.'
            )

        regresor = xgb.XGBRegressor()
        regresor.load_model(modelo.ruta_archivo)
        resultado = comparar_exactitud(train, test, predecir_con_modelo(regresor, test))
        tolerancia = resultado['modelo']['tolerancia_relativa']

        self._imprimir_por_categoria(resultado['modelo'], tolerancia)
        self._imprimir_comparacion(resultado, tolerancia)

        ruta = self._guardar_reporte(resultado, modelo, origen, aptas, no_aptas, dias_venta,
                                     dias_entrenamiento, dias_test, test)
        self._guardar_en_modelo(modelo, resultado)
        self.stdout.write(f'\nReporte guardado en {ruta}.')
        self.stdout.write(self.style.WARNING(
            f'No se activó ni registró ningún modelo; solo se guardaron los resultados en el '
            f'vigente (#{modelo.pk}).'
        ))

    def _imprimir_por_categoria(self, evaluacion, tolerancia):
        etiqueta_acierto = f'Acierto ±{tolerancia * 100:.0f}%'
        self.stdout.write('\nModelo vigente, por categoría:')
        encabezado = (
            f'{"Categoría":<12}{"Días":>5}{"Real":>9}{"Pronóst.":>10}{"MAE":>8}{"WAPE":>9}'
            f'{"Exactitud":>11}{etiqueta_acierto:>14}{"R² intra":>10}'
        )
        self.stdout.write(encabezado)
        self.stdout.write('-' * len(encabezado))
        for categoria, m in evaluacion['categorias'].items():
            self.stdout.write(
                f'{categoria:<12}{m["dias"]:>5}{m["demanda_real"]:>9.0f}{m["demanda_pronosticada"]:>10.1f}'
                f'{_num(m["mae"], 2):>8}{_pct(m["wape"]):>9}{_pct(m["exactitud"]):>11}'
                f'{_pct(m["acierto_tolerancia"]):>14}{_num(m["r2"]):>10}'
            )
        t = evaluacion['total']
        self.stdout.write('-' * len(encabezado))
        self.stdout.write(
            f'{"Total":<12}{t["dias"]:>5}{t["demanda_real"]:>9.0f}{t["demanda_pronosticada"]:>10.1f}'
            f'{_num(t["mae"], 2):>8}{_pct(t["wape"]):>9}{_pct(t["exactitud"]):>11}'
            f'{_pct(t["acierto_tolerancia"]):>14}{"":>10}'
        )
        self.stdout.write(
            f'R² global (todas las filas juntas, incluye la diferencia de nivel entre categorías): '
            f'{_num(t["r2_global"])}. R² intra: calculado dentro de cada categoría. '
            'Total WAPE/exactitud: WAPE agregado (Σ|error| / Σ real), no el promedio por categoría. '
            'Días = filas de test de esa categoría.'
        )

    def _imprimir_comparacion(self, resultado, tolerancia):
        filas = [('modelo', 'Modelo vigente')] + [(c, nombre) for c, (nombre, _) in LINEAS_BASE.items()]
        categorias = list(resultado['modelo']['categorias'])
        etiqueta_acierto = f'Acierto ±{tolerancia * 100:.0f}%'

        self.stdout.write('\nModelo y líneas base sobre el mismo test (total):')
        encabezado = (
            f'{"":<38}{"MAE":>8}{"RMSE":>8}{"WAPE":>9}{"Exactitud":>11}{etiqueta_acierto:>14}{"R² global":>11}'
        )
        self.stdout.write(encabezado)
        self.stdout.write('-' * len(encabezado))
        for clave, nombre in filas:
            t = resultado[clave]['total']
            self.stdout.write(
                f'{nombre:<38}{_num(t["mae"], 2):>8}{_num(t["rmse"], 2):>8}{_pct(t["wape"]):>9}'
                f'{_pct(t["exactitud"]):>11}{_pct(t["acierto_tolerancia"]):>14}{_num(t["r2_global"]):>11}'
            )

        self.stdout.write('\nExactitud y R² intra-categoría:')
        encabezado = f'{"":<38}' + ''.join(f'{"Exact. " + c:>18}' for c in categorias) + \
            ''.join(f'{"R² " + c:>14}' for c in categorias)
        self.stdout.write(encabezado)
        self.stdout.write('-' * len(encabezado))
        for clave, nombre in filas:
            por_categoria = resultado[clave]['categorias']
            self.stdout.write(
                f'{nombre:<38}'
                + ''.join(f'{_pct(por_categoria[c]["exactitud"]):>18}' for c in categorias)
                + ''.join(f'{_num(por_categoria[c]["r2"]):>14}' for c in categorias)
            )

        mejor = resultado['mejor_linea_base']
        no_supera = [LINEAS_BASE[c][0] for c, supera in resultado['supera'].items() if not supera]
        self.stdout.write(f'\nMejor línea base por WAPE: {LINEAS_BASE[mejor][0]}.')
        if no_supera:
            self.stdout.write(self.style.WARNING(
                f'El modelo NO supera en WAPE a: {", ".join(no_supera)}. No puede presentarse '
                'como aportando valor frente a esas líneas base con este test.'
            ))
        else:
            self.stdout.write(self.style.SUCCESS('El modelo supera en WAPE a las tres líneas base.'))

    def _guardar_reporte(self, resultado, modelo, origen, aptas, no_aptas, dias_venta,
                         dias_entrenamiento, dias_test, test):
        DIR_REPORTES.mkdir(parents=True, exist_ok=True)
        ahora = timezone.now()
        ruta = DIR_REPORTES / f'evaluacion_categoria_{ahora:%Y%m%d_%H%M%S}.json'
        predicciones = resultado['predicciones'].copy()
        predicciones['fecha'] = predicciones['fecha'].dt.strftime('%Y-%m-%d')
        reporte = {
            'fecha': ahora.isoformat(),
            'origen': origen,
            'nivel': NivelPrediccion.CATEGORIA,
            'modelo_ajustado': modelo.pk,
            'ruta_modelo': modelo.ruta_archivo,
            'dias_entrenamiento': dias_entrenamiento,
            'dias_prueba': dias_test,
            'fechas_prueba': [test['fecha'].min().strftime('%Y-%m-%d'), test['fecha'].max().strftime('%Y-%m-%d')],
            'tipo_pronostico': 'a un paso (variables con la demanda real hasta el día anterior)',
            'categorias_evaluadas': aptas,
            'categorias_sin_pronostico': {c: dias_venta[c] for c in no_aptas},
            'tolerancia_relativa': resultado['modelo']['tolerancia_relativa'],
            'lineas_base': {clave: nombre for clave, (nombre, _) in LINEAS_BASE.items()},
            'resultados': {clave: resultado[clave] for clave in ['modelo', *LINEAS_BASE]},
            'mejor_linea_base': resultado['mejor_linea_base'],
            'modelo_supera_por_wape': resultado['supera'],
            'predicciones': predicciones.to_dict(orient='records'),
        }
        ruta.write_text(json.dumps(reporte, ensure_ascii=False, indent=2, default=str))
        return ruta

    def _guardar_en_modelo(self, modelo, resultado):
        """Guarda la corrida en el modelo vigente sin tocar activo, fase ni
        métricas de entrenamiento."""
        total = resultado['modelo']['total']
        mejor = resultado['mejor_linea_base']
        total_mejor = resultado[mejor]['total']
        modelo.fecha_evaluacion = timezone.now()
        modelo.wape = total['wape']
        modelo.exactitud = total['exactitud']
        modelo.r2_intra_categoria = total['r2_por_categoria']
        modelo.acierto_tolerancia = {
            c: m['acierto_tolerancia'] for c, m in resultado['modelo']['categorias'].items()
        }
        modelo.tolerancia_relativa = resultado['modelo']['tolerancia_relativa']
        modelo.mejor_linea_base = mejor
        modelo.wape_mejor_linea_base = total_mejor['wape']
        modelo.exactitud_mejor_linea_base = total_mejor['exactitud']
        modelo.metricas_mejor_linea_base = resultado[mejor]
        modelo.save(update_fields=[
            'fecha_evaluacion', 'wape', 'exactitud', 'r2_intra_categoria', 'acierto_tolerancia',
            'tolerancia_relativa', 'mejor_linea_base', 'wape_mejor_linea_base',
            'exactitud_mejor_linea_base', 'metricas_mejor_linea_base',
        ])
