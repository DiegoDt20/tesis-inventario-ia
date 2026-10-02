"""Comando de gestión: completa la demanda real de predicciones vencidas y
reporta las métricas del motor de predicción de demanda ya en producción.

Además evalúa la exactitud del pronóstico (WAPE, exactitud, acierto dentro
de tolerancia, R² por categoría) del modelo ajustado vigente contra tres
líneas base (ver inventario/ml/evaluacion.py), en dos horizontes: diario y
ventana de lead time (suma de los días que tarda en llegar un pedido).

- Por defecto, con validación de origen móvil (inventario/ml/walk_forward.py):
  el ajuste fino del modelo vigente se rehace en cada origen solo con los
  días anteriores y se pronostica encadenando, como predecir_demanda.
- Con --no-walk-forward, la división única de antes (ModeloEntrenado
  .dias_prueba) con el archivo del modelo vigente, para reproducir
  resultados anteriores; solo esa corrida diaria se guarda en el modelo.

No activa ni registra ningún modelo. El reporte va a artefactos/evaluacion/.
"""
import argparse
import json
from pathlib import Path

import pandas as pd
import xgboost as xgb
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Max, Sum
from django.utils import timezone

from inventario.ml.carga_interna import (
    MIN_DIAS_VENTA_CATEGORIA,
    leer_demanda_interna_por_categoria,
    seleccionar_categorias_aptas,
)
from inventario.ml.entrenamiento import HIPERPARAMETROS_AJUSTE
from inventario.ml.evaluacion import LINEAS_BASE, calcular_metricas, comparar_exactitud, predecir_con_modelo
from inventario.ml.features import construir_features, dividir_temporal
from inventario.ml.walk_forward import (
    HORIZONTE_DIARIO,
    MIN_ORIGENES_CONFIABLE,
    HistoricoInsuficiente,
    ValidacionOrigenMovil,
    agregar_por_ventana,
    horizonte_lead_time,
    resumir,
)
from inventario.models import ModeloEntrenado, NivelPrediccion, Origen, Pedido, PedidoDetalle, Prediccion

DIR_REPORTES = Path('artefactos/evaluacion')

HORIZONTES = ['diario', 'ventana', 'ambos']


def _pct(valor, decimales=1):
    """Fracción como porcentaje, o '—' si no está definida."""
    return '—' if valor is None else f'{valor * 100:.{decimales}f}%'


def _num(valor, decimales=4):
    return '—' if valor is None else f'{valor:.{decimales}f}'


def _pp(valor):
    """Desviación de una fracción, en puntos porcentuales."""
    return '—' if valor is None else f'±{valor * 100:.1f} pp'


def _fecha(valor):
    return pd.Timestamp(valor).strftime('%Y-%m-%d')


def _registros(df):
    """DataFrame a lista de dicts con las fechas como texto (para el JSON)."""
    df = df.copy()
    for columna in ('origen', 'fecha', 'fecha_inicio', 'fecha_fin'):
        if columna in df:
            df[columna] = pd.to_datetime(df[columna]).dt.strftime('%Y-%m-%d')
    return df.to_dict(orient='records')


class Command(BaseCommand):
    help = (
        'Completa demanda_real en las predicciones cuya fecha_objetivo ya '
        'pasó (a partir de PedidoDetalle.cantidad_solicitada) y calcula MAE, '
        'RMSE, SMAPE y R² sobre todas las predicciones ya evaluadas. Además '
        'evalúa la exactitud (WAPE, acierto dentro de tolerancia, R² por '
        'categoría) del modelo ajustado vigente contra tres líneas base, en '
        'horizonte diario y de ventana de lead time, con validación de origen móvil.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--origen', default=Origen.REAL, choices=Origen.values,
            help='Origen de los pedidos con que se mide la demanda real (por defecto "real").',
        )
        parser.add_argument(
            '--horizonte', default='ambos', choices=HORIZONTES,
            help=(
                '"diario" (cada día por separado), "ventana" (suma de los días del lead time '
                'que usa el motor de decisiones) o "ambos" (por defecto).'
            ),
        )
        parser.add_argument(
            '--walk-forward', default=True, action=argparse.BooleanOptionalAction,
            help=(
                'Validación de origen móvil (por defecto). Con --no-walk-forward, la división '
                'única de antes (días de prueba del modelo vigente), para reproducir resultados anteriores.'
            ),
        )

    def handle(self, *args, **options):
        origen = options['origen']
        self._completar_demanda_real(origen)
        self._metricas_predicciones()

        contexto = self._preparar_exactitud(origen, options['walk_forward'])
        if contexto is None:
            return
        horizontes = {}
        if options['horizonte'] in ('diario', 'ambos'):
            horizontes['diario'] = HORIZONTE_DIARIO
        if options['horizonte'] in ('ventana', 'ambos'):
            horizontes['ventana'] = self._horizonte_ventana(contexto)

        if options['walk_forward']:
            secciones = self._walk_forward(contexto, horizontes)
        else:
            secciones = self._division_unica(contexto, horizontes)

        ruta = self._guardar_reporte(contexto, secciones, options['walk_forward'])
        self.stdout.write(f'\nReporte guardado en {ruta}.')
        guardado = (
            f'solo se guardaron los resultados diarios en el vigente (#{contexto["modelo"].pk}).'
            if not options['walk_forward'] and 'diario' in horizontes
            else 'los resultados quedan solo en el reporte.'
        )
        self.stdout.write(self.style.WARNING(f'No se activó ni registró ningún modelo; {guardado}'))

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

    def _preparar_exactitud(self, origen, walk_forward):
        """Modelo vigente, modelo base (solo walk-forward) y demanda por
        categoría apta. Devuelve None (con una advertencia) si falta algo
        para evaluar."""
        modo = 'validación de origen móvil' if walk_forward else 'división única del modelo vigente'
        self.stdout.write(self.style.SUCCESS(f'\nExactitud del pronóstico ({modo})'))
        modelo = ModeloEntrenado.objects.filter(
            fase=ModeloEntrenado.Fase.AJUSTADO, activo=True, nivel=NivelPrediccion.CATEGORIA,
        ).first()
        if modelo is None:
            self.stdout.write(self.style.WARNING(
                '  No hay un modelo ajustado activo a nivel de categoría; se omite.'
            ))
            return None
        base = None
        if walk_forward:
            base = ModeloEntrenado.objects.filter(fase=ModeloEntrenado.Fase.BASE, activo=True).first()
            if base is None:
                raise CommandError(
                    'No hay un modelo base activo: la validación de origen móvil rehace el ajuste '
                    'fino sobre él. Corre "entrenar_base" o usa --no-walk-forward.'
                )
        elif not modelo.dias_prueba:
            self.stdout.write(self.style.WARNING(
                f'  El modelo #{modelo.pk} no registra sus días de prueba; no se puede '
                'reconstruir su test temporal. Se omite.'
            ))
            return None
        if modelo.origen_datos_internos and modelo.origen_datos_internos != origen:
            self.stdout.write(self.style.WARNING(
                f'  Ojo: el modelo #{modelo.pk} se ajustó con datos de origen '
                f'"{modelo.origen_datos_internos}" y se evalúa con "{origen}".'
            ))

        df = leer_demanda_interna_por_categoria(origen=origen)
        if df.empty:
            self.stdout.write(self.style.WARNING(f'  No hay pedidos de origen "{origen}"; se omite.'))
            return None
        # Mismas categorías que entrenar_ajustado: el modelo solo pronostica
        # las aptas; las demás se gestionan con punto de reorden.
        aptas, no_aptas, dias_venta = seleccionar_categorias_aptas(df, min_dias_venta=MIN_DIAS_VENTA_CATEGORIA)
        df = df[df['serie_id'].isin(aptas)].reset_index(drop=True)

        self.stdout.write(f'  Categorías evaluadas: {", ".join(aptas)}.')
        if no_aptas:
            fuera = ', '.join(f'{c} ({dias_venta[c]} días con venta)' for c in no_aptas)
            self.stdout.write(
                f'  Sin pronóstico del modelo (menos de {MIN_DIAS_VENTA_CATEGORIA} días con venta; '
                f'solo punto de reorden): {fuera}.'
            )
        return {
            'origen': origen, 'modelo': modelo, 'base': base, 'df': df,
            'aptas': aptas, 'no_aptas': no_aptas, 'dias_venta': dias_venta,
        }

    def _horizonte_ventana(self, contexto):
        try:
            horizonte, lead_times = horizonte_lead_time(contexto['aptas'])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        detalle = ', '.join(f'{lt:g} días en {n}' for lt, n in lead_times.items())
        self.stdout.write(
            f'  Ventana de lead time: H = {horizonte} días (mediana, redondeada hacia arriba, del lead '
            f'time que usa el motor de decisiones para el stock de seguridad en los '
            f'{sum(lead_times.values())} productos activos de las categorías evaluadas: {detalle}).'
        )
        contexto['lead_times'] = lead_times
        return horizonte

    def _walk_forward(self, contexto, horizontes):
        modelo = contexto['modelo']
        hiperparametros = modelo.hiperparametros or HIPERPARAMETROS_AJUSTE
        regresor_base = xgb.XGBRegressor()
        regresor_base.load_model(contexto['base'].ruta_archivo)
        validacion = ValidacionOrigenMovil(
            contexto['df'], booster_base=regresor_base.get_booster(), hiperparametros_ajuste=hiperparametros,
        )
        # Se verifica todo antes de entrenar nada.
        for horizonte in horizontes.values():
            if not validacion.origenes(horizonte):
                raise CommandError(
                    f'No hay ningún origen evaluable con horizonte {horizonte}: el histórico de origen '
                    f'"{contexto["origen"]}" cubre {len(validacion.fechas)} día(s) y se necesitan al menos '
                    f'{validacion.min_dias_ajuste} de ajuste más {horizonte} de pronóstico.'
                )
        self.stdout.write(
            f'  Ajuste fino por origen con la configuración del vigente (#{modelo.pk}: {hiperparametros}) '
            f'sobre el modelo base #{contexto["base"].pk}, que no se reentrena. Pronóstico encadenado.'
        )
        contexto['hiperparametros'] = hiperparametros

        secciones = {}
        for nombre, horizonte in horizontes.items():
            try:
                ventanas, diario = validacion.evaluar(horizonte)
            except HistoricoInsuficiente as exc:
                raise CommandError(str(exc)) from exc
            origenes = validacion.origenes(horizonte)
            resultado = resumir(ventanas)
            self._imprimir_horizonte(nombre, horizonte, resultado, origenes, len(ventanas))
            secciones[nombre] = {
                'horizonte_dias': horizonte,
                'origenes': [_fecha(o) for o in origenes],
                'n_origenes': len(origenes),
                'resultado': resultado,
                'ventanas': ventanas,
                'diario': diario,
            }
        return secciones

    def _division_unica(self, contexto, horizontes):
        """La división de entrenamiento del vigente: diario a un paso (lo de
        antes) y, para la ventana, un único origen (el último día de
        entrenamiento) pronosticado de forma encadenada con el archivo del
        vigente."""
        modelo, df = contexto['modelo'], contexto['df']
        dias_test = modelo.dias_prueba
        n_dias = (df['fecha'].max() - df['fecha'].min()).days + 1
        dias_entrenamiento = n_dias - dias_test
        train, test = dividir_temporal(construir_features(df), dias_test=dias_test)

        self.stdout.write(
            f'  Modelo #{modelo.pk} ({modelo.ruta_archivo}), origen={contexto["origen"]}. '
            f'Entrenamiento {train["fecha"].min():%d/%m}–{train["fecha"].max():%d/%m} '
            f'({dias_entrenamiento} días), prueba {test["fecha"].min():%d/%m}–{test["fecha"].max():%d/%m} '
            f'({dias_test} días, {len(test)} filas).'
        )
        if modelo.dias_entrenamiento is not None and modelo.dias_entrenamiento != dias_entrenamiento:
            self.stdout.write(self.style.WARNING(
                f'  Ojo: el modelo se entrenó con {modelo.dias_entrenamiento} días y hoy los datos '
                f'dan {dias_entrenamiento}: el histórico cambió desde el entrenamiento.'
            ))
        contexto.update(dias_entrenamiento=dias_entrenamiento, dias_prueba=dias_test)

        regresor = xgb.XGBRegressor()
        regresor.load_model(modelo.ruta_archivo)
        secciones = {}
        if 'diario' in horizontes:
            self.stdout.write('\nHorizonte diario, pronóstico a un paso:')
            resultado = comparar_exactitud(train, test, predecir_con_modelo(regresor, test))
            tolerancia = resultado['modelo']['tolerancia_relativa']
            self._imprimir_por_categoria(resultado['modelo'], tolerancia)
            self._imprimir_comparacion(resultado, tolerancia)
            self._guardar_en_modelo(modelo, resultado)
            secciones['diario'] = {
                'horizonte_dias': HORIZONTE_DIARIO,
                'fechas_prueba': [_fecha(test['fecha'].min()), _fecha(test['fecha'].max())],
                'tipo_pronostico': 'a un paso (variables con la demanda real hasta el día anterior)',
                'resultado': resultado,
                'diario': resultado.pop('predicciones'),
            }
        if 'ventana' in horizontes:
            horizonte = horizontes['ventana']
            origen_unico = train['fecha'].max()
            if origen_unico + pd.Timedelta(days=horizonte) > df['fecha'].max():
                raise CommandError(
                    f'La ventana de {horizonte} días desde el {origen_unico:%d/%m} pasa del último día '
                    'con datos; la división única no alcanza para evaluarla.'
                )
            validacion = ValidacionOrigenMovil(df, ajustar=lambda _train: regresor)
            diario = validacion.pronosticar(origen_unico, horizonte)
            ventanas = agregar_por_ventana(diario)
            resultado = resumir(ventanas)
            self._imprimir_horizonte('ventana', horizonte, resultado, [origen_unico], len(ventanas))
            secciones['ventana'] = {
                'horizonte_dias': horizonte,
                'origenes': [_fecha(origen_unico)],
                'n_origenes': 1,
                'resultado': resultado,
                'ventanas': ventanas,
                'diario': diario,
            }
        return secciones

    def _imprimir_horizonte(self, nombre, horizonte, resultado, origenes, n_filas):
        """Tabla de un horizonte: por cada método (modelo y líneas base), sus
        categorías y el total, con la dispersión del WAPE entre orígenes."""
        if nombre == 'diario':
            titulo = f'Horizonte diario (H = {horizonte})'
            unidad = 'día(s)'
        else:
            titulo = f'Horizonte ventana de lead time (H = {horizonte} días, suma de la ventana)'
            unidad = 'ventana(s)'
        self.stdout.write(self.style.SUCCESS(
            f'\n{titulo}: {len(origenes)} origen(es) ({origenes[0]:%d/%m}–{origenes[-1]:%d/%m}), '
            f'{n_filas} {unidad} categoría.'
        ))
        if len(origenes) < MIN_ORIGENES_CONFIABLE:
            self.stdout.write(self.style.WARNING(
                f'  Solo {len(origenes)} origen(es) evaluado(s), menos de {MIN_ORIGENES_CONFIABLE}: '
                'la métrica sigue siendo frágil.'
            ))

        tolerancia = resultado['modelo']['tolerancia_relativa']
        etiqueta_acierto = f'Acierto ±{tolerancia * 100:.0f}%'
        encabezado = (
            f'{"Método":<38}{"Categoría":<19}{"Filas":>6}{"Real":>9}{"Pronóst.":>10}{"MAE":>8}{"RMSE":>8}'
            f'{"WAPE":>9}{"Exactitud":>11}{etiqueta_acierto:>14}{"R²":>9}{"Desv. WAPE":>12}'
        )
        self.stdout.write(encabezado)
        self.stdout.write('-' * len(encabezado))
        metodos = [('modelo', 'Modelo vigente')] + [(c, nombre) for c, (nombre, _) in LINEAS_BASE.items()]
        for clave, etiqueta in metodos:
            evaluacion = resultado[clave]
            dispersion = resultado['dispersion'][clave]
            filas = [
                (categoria, m, m['r2'], dispersion['por_categoria'][categoria]['desviacion'])
                for categoria, m in evaluacion['categorias'].items()
            ]
            total = evaluacion['total']
            filas.append(('Total (R² global)', total, total['r2_global'], dispersion['desviacion']))
            for i, (categoria, m, r2, desviacion) in enumerate(filas):
                self.stdout.write(
                    f'{etiqueta if i == 0 else "":<38}{categoria:<19}{m["dias"]:>6}{m["demanda_real"]:>9.0f}'
                    f'{m["demanda_pronosticada"]:>10.1f}{_num(m["mae"], 2):>8}{_num(m["rmse"], 2):>8}'
                    f'{_pct(m["wape"]):>9}{_pct(m["exactitud"]):>11}{_pct(m["acierto_tolerancia"]):>14}'
                    f'{_num(r2, 3):>9}{_pp(desviacion):>12}'
                )
            self.stdout.write('-' * len(encabezado))
        self.stdout.write(
            f'Filas = {unidad} evaluadas (orígenes × categorías). R² = intra-categoría en las filas de '
            'categoría y global en Total. WAPE total = agregado (Σ|error| / Σ real). Desv. WAPE = '
            'desviación estándar del WAPE entre orígenes (uno por origen, no por fila).'
        )

        self.stdout.write('\nDispersión entre orígenes (WAPE total de cada origen):')
        for clave, etiqueta in metodos:
            d = resultado['dispersion'][clave]
            self.stdout.write(
                f'  {etiqueta:<38}media {_pct(d["media"])} {_pp(d["desviacion"])} '
                f'(mín. {_pct(d["minimo"])}, máx. {_pct(d["maximo"])}; {d["n_origenes"]} orígenes)'
            )

        mejor = resultado['mejor_linea_base']
        no_supera = [LINEAS_BASE[c][0] for c, supera in resultado['supera'].items() if not supera]
        self.stdout.write(f'Mejor línea base por WAPE: {LINEAS_BASE[mejor][0]}.')
        if no_supera:
            self.stdout.write(self.style.WARNING(
                f'El modelo NO supera en WAPE a: {", ".join(no_supera)}.'
                + (' En el horizonte de ventana, el que importa para la decisión, no puede presentarse '
                   'como aportando valor.' if nombre == 'ventana' else '')
            ))
        else:
            self.stdout.write(self.style.SUCCESS('El modelo supera en WAPE a las tres líneas base.'))

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

    def _guardar_reporte(self, contexto, secciones, walk_forward):
        DIR_REPORTES.mkdir(parents=True, exist_ok=True)
        ahora = timezone.now()
        modo = 'walkforward' if walk_forward else 'division_unica'
        ruta = DIR_REPORTES / f'evaluacion_{modo}_{ahora:%Y%m%d_%H%M%S}.json'
        modelo = contexto['modelo']
        horizontes = {}
        for nombre, seccion in secciones.items():
            resultado = seccion['resultado']
            horizontes[nombre] = {
                **{k: v for k, v in seccion.items() if k not in ('resultado', 'ventanas', 'diario')},
                'resultados': {clave: resultado[clave] for clave in ['modelo', *LINEAS_BASE]},
                'dispersion_entre_origenes': resultado.get('dispersion'),
                'mejor_linea_base': resultado['mejor_linea_base'],
                'modelo_supera_por_wape': resultado['supera'],
                'ventanas': _registros(seccion['ventanas']) if 'ventanas' in seccion else None,
                'pronosticos_diarios': _registros(seccion['diario']),
            }
        reporte = {
            'fecha': ahora.isoformat(),
            'modo': 'validacion de origen movil' if walk_forward else 'division unica',
            'origen': contexto['origen'],
            'nivel': NivelPrediccion.CATEGORIA,
            'modelo_ajustado': modelo.pk,
            'ruta_modelo': modelo.ruta_archivo,
            'modelo_base': contexto['base'].pk if contexto['base'] else None,
            'hiperparametros_ajuste': contexto.get('hiperparametros'),
            'dias_entrenamiento': contexto.get('dias_entrenamiento'),
            'dias_prueba': contexto.get('dias_prueba'),
            'lead_times_productos': contexto.get('lead_times'),
            'categorias_evaluadas': contexto['aptas'],
            'categorias_sin_pronostico': {c: contexto['dias_venta'][c] for c in contexto['no_aptas']},
            'tolerancia_relativa': next(iter(secciones.values()))['resultado']['modelo']['tolerancia_relativa'],
            'lineas_base': {clave: nombre for clave, (nombre, _) in LINEAS_BASE.items()},
            'horizontes': horizontes,
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
