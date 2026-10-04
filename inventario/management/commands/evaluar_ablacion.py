"""Comando de gestión: estudio de ablación de las fuentes de datos del
pronóstico (ver inventario/ml/ablacion.py).

Evalúa, sobre la misma validación de origen móvil que evaluar_predicciones
(mismos orígenes, filas y horizontes), el modelo base sin ajuste con datos
de la empresa (A, con y sin escalado), un modelo solo con datos internos (B)
y el procedimiento del vigente (C), junto a las tres líneas base. No toca
datos ni activa ni registra modelos. El reporte va a artefactos/ablacion/.
"""
import json
from pathlib import Path

import pandas as pd
import xgboost as xgb
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from inventario.ml import ablacion
from inventario.ml.carga_interna import (
    MIN_DIAS_VENTA_CATEGORIA,
    leer_demanda_interna_por_categoria,
    seleccionar_categorias_aptas,
)
from inventario.ml.evaluacion import LINEAS_BASE
from inventario.ml.walk_forward import HORIZONTE_DIARIO, MIN_ORIGENES_CONFIABLE, horizonte_lead_time
from inventario.models import ModeloEntrenado, NivelPrediccion, Origen

DIR_REPORTES = Path('artefactos/ablacion')

ETIQUETAS = {**ablacion.CONFIGURACIONES, **{clave: nombre for clave, (nombre, _) in LINEAS_BASE.items()}}

PRECISION = (
    '"Sin datos de la empresa" no es posible en sentido estricto: para pronosticar, el modelo necesita los '
    'rezagos y medias móviles de la propia serie, que vienen de agosto. A es "sin ajuste con datos de la '
    'empresa": el base no se reentrena con agosto, pero recibe el historial de agosto hasta cada origen '
    'como entrada.'
)


def _pct(valor, signo=False):
    return '—' if valor is None else f'{valor * 100:{"+" if signo else ""}.1f}%'


def _num(valor, decimales=3):
    return '—' if valor is None else f'{valor:.{decimales}f}'


def _pp(valor):
    return '—' if valor is None else f'±{valor * 100:.1f} pp'


def _registros(df):
    df = df.copy()
    for columna in ('origen', 'fecha', 'fecha_inicio', 'fecha_fin'):
        if columna in df:
            df[columna] = pd.to_datetime(df[columna]).dt.strftime('%Y-%m-%d')
    return df.to_dict(orient='records')


class Command(BaseCommand):
    help = (
        'Estudio de ablación: evalúa el modelo base sin ajuste con datos de la empresa (con y sin '
        'escalado), un modelo solo con datos internos y el procedimiento del vigente, con las tres '
        'líneas base, sobre la misma validación de origen móvil. No activa ni registra modelos.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--origen', default=Origen.REAL, choices=Origen.values,
            help='Origen de los pedidos con que se mide la demanda (por defecto "real").',
        )

    def handle(self, *args, **options):
        ctx = self._preparar(options['origen'])
        regresor_base = xgb.XGBRegressor()
        regresor_base.load_model(ctx['base'].ruta_archivo)
        escalado = ctx['vigente'].escalado
        validaciones = ablacion.construir_validaciones(
            ctx['df'], regresor_base, ctx['hiperparametros'], escalado['nivel_minimo'], escalado['nivel_referencia'],
        )

        horizontes = {'diario': HORIZONTE_DIARIO, 'ventana': ctx['horizonte']}
        secciones = {}
        for nombre, horizonte in horizontes.items():
            origenes = validaciones['C'].origenes(horizonte)
            if not origenes:
                raise CommandError(
                    f'No hay ningún origen evaluable con horizonte {horizonte}: el histórico cubre '
                    f'{len(validaciones["C"].fechas)} día(s).'
                )
            diario, ventanas = ablacion.evaluar(validaciones, horizonte)
            resumen = ablacion.resumir_ablacion(ventanas)
            self._imprimir_horizonte(nombre, horizonte, origenes, resumen)
            secciones[nombre] = {
                'horizonte': horizonte, 'origenes': origenes, 'resumen': resumen,
                'diario': diario, 'ventanas': ventanas,
            }

        lectura = self._lectura(secciones['ventana']['resumen'], secciones['diario']['resumen'])
        ruta = self._guardar(ctx, secciones, lectura, validaciones)
        self.stdout.write(f'\nReporte guardado en {ruta}.')
        self.stdout.write(self.style.WARNING('No se tocaron datos ni se activó ni registró ningún modelo.'))

    def _preparar(self, origen):
        vigente = ModeloEntrenado.objects.filter(
            fase=ModeloEntrenado.Fase.AJUSTADO, activo=True, nivel=NivelPrediccion.CATEGORIA,
        ).first()
        base = ModeloEntrenado.objects.filter(fase=ModeloEntrenado.Fase.BASE, activo=True).first()
        if vigente is None or base is None:
            raise CommandError('Hacen falta un modelo base activo y un modelo ajustado activo a nivel de categoría.')
        if not vigente.escalado:
            raise CommandError(
                f'El vigente #{vigente.pk} no se ajustó con escalado de nivel: la ablación compara contra el '
                'procedimiento con escalado (C) y toma de él la regla y los niveles para A con escalado.'
            )
        df = leer_demanda_interna_por_categoria(origen=origen)
        if df.empty:
            raise CommandError(f'No hay pedidos de origen "{origen}".')
        aptas, no_aptas, _ = seleccionar_categorias_aptas(df, min_dias_venta=MIN_DIAS_VENTA_CATEGORIA)
        df = df[df['serie_id'].isin(aptas)].reset_index(drop=True)
        try:
            horizonte, _ = horizonte_lead_time(aptas)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        hiperparametros = vigente.hiperparametros

        self.stdout.write(self.style.SUCCESS(
            f'Estudio de ablación: origen {origen}, {df["fecha"].min():%d/%m/%Y}–{df["fecha"].max():%d/%m/%Y}, '
            f'categorías {", ".join(aptas)} (sin pronóstico del modelo: {", ".join(no_aptas) or "ninguna"}).'
        ))
        self.stdout.write(f'Precisión: {PRECISION}')
        self.stdout.write(
            f'Configuraciones (fijadas antes de correr):\n'
            f'  A: base #{base.pk} tal cual, sin escalado.\n'
            f'  A con escalado: base #{base.pk} con la regla del vigente (mínimo {vigente.escalado["nivel_minimo"]:.2f}, '
            f'referencia {vigente.escalado["nivel_referencia"]:.2f} u/día; factores con los días <= origen).\n'
            f'  B: XGBoost desde cero con los días <= origen, hiperparámetros del ajuste del vigente #{vigente.pk} '
            f'({hiperparametros}), sin escalado (no hay nivel externo que igualar).\n'
            f'  C: procedimiento del vigente #{vigente.pk}: ajuste fino del base #{base.pk} con escalado.\n'
            f'  Ventana de lead time: H = {horizonte} días.'
        )
        self.stdout.write(
            '  Nota: con 50 árboles y learning rate 0.01 desde cero, B parte de la media global (de todas las '
            'categorías) y solo puede recorrer una parte del camino hacia el nivel de cada una. Es la '
            'consecuencia de usar la misma configuración que C, no un ajuste a favor o en contra.'
        )
        return {'origen': origen, 'vigente': vigente, 'base': base, 'df': df, 'aptas': aptas,
                'horizonte': horizonte, 'hiperparametros': hiperparametros}

    def _imprimir_horizonte(self, nombre, horizonte, origenes, resumen):
        titulo = 'diario (H = 1)' if nombre == 'diario' else f'ventana de lead time (H = {horizonte} días, suma)'
        self.stdout.write(self.style.SUCCESS(
            f'\nHorizonte {titulo}: {len(origenes)} orígenes ({origenes[0]:%d/%m}–{origenes[-1]:%d/%m}).'
        ))
        if len(origenes) < MIN_ORIGENES_CONFIABLE:
            self.stdout.write(self.style.WARNING(
                f'  Solo {len(origenes)} origen(es), menos de {MIN_ORIGENES_CONFIABLE}: la métrica sigue siendo frágil.'
            ))
        etiqueta_acierto = 'Acierto ±' + f'{resumen["metodos"]["C"]["exactitud"]["tolerancia_relativa"] * 100:.0f}%'
        encabezado = (
            f'{"Configuración":<54}{"Categoría":<12}{"Real":>8}{"Pronóst.":>10}{"Sesgo":>9}{"WAPE":>9}'
            f'{"Exactitud":>11}{etiqueta_acierto:>14}{"R²":>9}{"Desv. WAPE":>12}'
        )
        self.stdout.write(encabezado)
        self.stdout.write('-' * len(encabezado))
        for clave in ablacion.COLUMNAS:
            metodo = resumen['metodos'][clave]
            exactitud, sesgo, dispersion = metodo['exactitud'], metodo['sesgo'], metodo['dispersion']
            filas = [
                (categoria, m, m['r2'], sesgo[categoria]['sesgo_relativo'],
                 dispersion['por_categoria'][categoria]['desviacion'])
                for categoria, m in exactitud['categorias'].items()
            ]
            total = exactitud['total']
            sesgo_total = (
                (total['demanda_pronosticada'] - total['demanda_real']) / total['demanda_real']
                if total['demanda_real'] else None
            )
            filas.append(('Total', total, total['r2_global'], sesgo_total, dispersion['desviacion']))
            for i, (categoria, m, r2, sesgo_rel, desviacion) in enumerate(filas):
                self.stdout.write(
                    f'{ETIQUETAS[clave] if i == 0 else "":<54}{categoria:<12}{m["demanda_real"]:>8.0f}'
                    f'{m["demanda_pronosticada"]:>10.1f}{_pct(sesgo_rel, signo=True):>9}{_pct(m["wape"]):>9}'
                    f'{_pct(m["exactitud"]):>11}{_pct(m["acierto_tolerancia"]):>14}{_num(r2):>9}{_pp(desviacion):>12}'
                )
            self.stdout.write('-' * len(encabezado))
        self.stdout.write(
            'R² = intra-categoría en las filas de categoría y global en Total. Sesgo = Σ(pronóstico − real) / Σ real. '
            'Desv. WAPE = desviación del WAPE total entre orígenes.'
        )

        self.stdout.write('\nComparación origen por origen (WAPE total de cada origen):')
        for c in resumen['comparaciones'][:len(ablacion.PARES)]:
            self._imprimir_comparacion(c)
        self.stdout.write('  Contra la media por categoría:')
        for c in resumen['comparaciones'][len(ablacion.PARES):]:
            if c['contra'] == 'media_categoria':
                self._imprimir_comparacion(c)
        self.stdout.write(
            '  Los orígenes consecutivos se solapan (comparten casi todo el histórico de ajuste'
            + (' y casi todos sus días pronosticados' if horizonte > 1 else '')
            + '): los p indican consistencia, no son prueba de significancia.'
        )

    def _imprimir_comparacion(self, c):
        empates = f', {c["empates"]} empate(s)' if c['empates'] else ''
        self.stdout.write(
            f'  {c["configuracion"]:<11} vs. {c["contra"]:<16}gana en {c["gana"]:>2} de {c["origenes"]}{empates} '
            f'(p bilateral = {_num(c["p_bilateral"])}, unilateral = {_num(c["p_unilateral"])})  — {c["mide"]}'
        )

    def _lectura(self, ventana, diario):
        """Una línea por pregunta, con el criterio fijo: en la ventana, ¿WAPE
        total menor que el de las tres líneas base?"""
        mejor_base = min(LINEAS_BASE, key=lambda b: ablacion.wape_total(ventana, b))
        wape_mejor = ablacion.wape_total(ventana, mejor_base)

        def estado(clave):
            supera = ablacion.supera_lineas_base(ventana, clave)
            vs_media = ablacion.comparacion(ventana, clave, 'media_categoria')
            return supera, (
                f'WAPE ventana {_pct(ablacion.wape_total(ventana, clave))} vs. {_pct(wape_mejor)} de la mejor '
                f'línea base ({LINEAS_BASE[mejor_base][0]}); gana a la media por categoría en {vs_media["gana"]} de '
                f'{vs_media["origenes"]} orígenes (p = {_num(vs_media["p_bilateral"])}); diario '
                f'{_pct(ablacion.wape_total(diario, clave))}'
            )

        a, texto_a = estado('A')
        a_esc, texto_a_esc = estado('A_escalado')
        b, texto_b = estado('B')
        c, texto_c = estado('C')
        c_vs_a = ablacion.comparacion(ventana, 'C', 'A')
        c_vs_b = ablacion.comparacion(ventana, 'C', 'B')
        c_vs_a_esc = ablacion.comparacion(ventana, 'C', 'A_escalado')
        mejor_que_a_esc = ablacion.wape_total(ventana, 'C') < ablacion.wape_total(ventana, 'A_escalado')
        mejor_que_a = ablacion.wape_total(ventana, 'C') < ablacion.wape_total(ventana, 'A')
        mejor_que_b = ablacion.wape_total(ventana, 'C') < ablacion.wape_total(ventana, 'B')

        def si_no(valor):
            return 'SÍ' if valor else 'NO'

        lineas = [
            f'1. ¿Funciona sin ajuste con datos de la empresa (A)? {si_no(a)}: {texto_a}. '
            f'Con escalado: {si_no(a_esc)}: {texto_a_esc}.',
            f'2. ¿Funciona sin la transferencia desde Kaggle (B)? {si_no(b)}: {texto_b}.',
            f'3. ¿La combinación (C) supera a cada fuente por separado? '
            f'contra A: {si_no(mejor_que_a)} (WAPE ventana {_pct(ablacion.wape_total(ventana, "C"))} vs. '
            f'{_pct(ablacion.wape_total(ventana, "A"))}; gana en {c_vs_a["gana"]} de {c_vs_a["origenes"]} orígenes, '
            f'p = {_num(c_vs_a["p_bilateral"])}); contra B: {si_no(mejor_que_b)} '
            f'({_pct(ablacion.wape_total(ventana, "C"))} vs. {_pct(ablacion.wape_total(ventana, "B"))}; gana en '
            f'{c_vs_b["gana"]} de {c_vs_b["origenes"]}, p = {_num(c_vs_b["p_bilateral"])}). C supera a las tres '
            f'líneas base: {si_no(c)}. Contra A con escalado (aísla el ajuste con datos de la empresa, ambos '
            f'escalados): {si_no(mejor_que_a_esc)} en el agregado ({_pct(ablacion.wape_total(ventana, "C"))} vs. '
            f'{_pct(ablacion.wape_total(ventana, "A_escalado"))}), gana en {c_vs_a_esc["gana"]} de '
            f'{c_vs_a_esc["origenes"]} orígenes (p = {_num(c_vs_a_esc["p_bilateral"])}).',
        ]
        self.stdout.write(self.style.SUCCESS(
            '\nLectura (criterio fijo: en la ventana de lead time, WAPE total menor que el de las tres líneas base):'
        ))
        for linea in lineas:
            self.stdout.write(linea)
        return {'A': a, 'A_escalado': a_esc, 'B': b, 'C': c, 'C_mejor_que_A': mejor_que_a,
                'C_mejor_que_B': mejor_que_b, 'C_mejor_que_A_escalado': mejor_que_a_esc, 'texto': lineas}

    def _guardar(self, ctx, secciones, lectura, validaciones):
        DIR_REPORTES.mkdir(parents=True, exist_ok=True)
        ahora = timezone.now()
        ruta = DIR_REPORTES / f'ablacion_{ahora:%Y%m%d_%H%M%S}.json'
        reporte = {
            'fecha': ahora.isoformat(),
            'origen': ctx['origen'],
            'precision': PRECISION,
            'configuraciones': ablacion.CONFIGURACIONES,
            'modelo_base': ctx['base'].pk,
            'modelo_vigente': ctx['vigente'].pk,
            'hiperparametros_b_y_c': ctx['hiperparametros'],
            'escalado': {k: v for k, v in ctx['vigente'].escalado.items() if k != 'factores'},
            'factores_por_origen': {
                clave: {pd.Timestamp(o).strftime('%Y-%m-%d'): f for o, f in validaciones[clave].factores.items()}
                for clave in ('A_escalado', 'C')
            },
            'categorias_evaluadas': ctx['aptas'],
            'horizontes': {
                nombre: {
                    'horizonte_dias': s['horizonte'],
                    'origenes': [pd.Timestamp(o).strftime('%Y-%m-%d') for o in s['origenes']],
                    'resumen': s['resumen'],
                    'ventanas': _registros(s['ventanas']),
                    'pronosticos_diarios': _registros(s['diario']),
                }
                for nombre, s in secciones.items()
            },
            'lectura': lectura,
        }
        ruta.write_text(json.dumps(reporte, ensure_ascii=False, indent=2, default=str))
        return ruta
