"""Comando de gestión: diagnostica un sesgo sistemático del pronóstico en una
categoría (por defecto accesorio) con la misma validación de origen móvil
de evaluar_predicciones (ver inventario/ml/diagnostico_sesgo.py).

1. Caracteriza el sesgo: signo, orígenes con sobreestimación, error por
   posición dentro de la ventana y horizonte diario.
2. Contrasta las hipótesis (a) modelo base, (b) piso en cero, (c) días en
   cero y (d) mezcla de productos, cada una con una medición y un criterio
   fijo.
3. Solo si (a) queda identificada como causa dominante, evalúa la corrección
   que le corresponde (escalar la serie antes del ajuste fino,
   inventario/ml/escalado.py) con el mismo walk-forward y las mismas líneas
   base, y reporta el antes y el después de todas las categorías.

No toca datos ni activa ni registra modelos. El reporte va a
artefactos/diagnostico/.
"""
import json
from pathlib import Path

import pandas as pd
import xgboost as xgb
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from inventario.ml import diagnostico_sesgo as diag
from inventario.ml.carga_externa import RUTA_POR_DEFECTO, leer_datos_externos
from inventario.ml.carga_interna import (
    MIN_DIAS_VENTA_CATEGORIA,
    leer_demanda_interna_por_categoria,
    seleccionar_categorias_aptas,
)
from inventario.ml.entrenamiento import HIPERPARAMETROS_AJUSTE
from inventario.ml.escalado import EscaladoNivel, niveles_externos
from inventario.ml.evaluacion import LINEAS_BASE
from inventario.ml.walk_forward import (
    HORIZONTE_DIARIO,
    METODOS,
    HistoricoInsuficiente,
    ValidacionOrigenMovil,
    horizonte_lead_time,
    resumir,
)
from inventario.models import ModeloEntrenado, NivelPrediccion, Origen

DIR_REPORTES = Path('artefactos/diagnostico')

NOMBRES_HIPOTESIS = {
    'a': 'el sesgo viene del modelo base (Kaggle), no del ajuste fino',
    'b': 'el piso en cero introduce asimetría',
    'c': 'las medias/desviación móviles se degradan con muchos ceros',
    'd': 'la categoría mezcla productos de rotación muy distinta',
}


def _pct(valor, decimales=1, signo=False):
    if valor is None:
        return '—'
    return f'{valor * 100:{"+" if signo else ""}.{decimales}f}%'


def _num(valor, decimales=1, signo=False):
    return '—' if valor is None else f'{valor:{"+" if signo else ""}.{decimales}f}'


class Command(BaseCommand):
    help = (
        'Diagnostica el sesgo sistemático del pronóstico en una categoría (por defecto accesorio): '
        'lo caracteriza, contrasta cuatro hipótesis con una medición cada una y, solo si la del '
        'modelo base queda identificada como dominante, evalúa su corrección con el mismo walk-forward. '
        'No activa ni registra modelos.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--categoria', default='accesorio', help='Categoría a diagnosticar (por defecto accesorio).')
        parser.add_argument(
            '--origen', default=Origen.REAL, choices=Origen.values,
            help='Origen de los pedidos con que se mide la demanda (por defecto "real").',
        )
        parser.add_argument(
            '--archivo-externo', default=RUTA_POR_DEFECTO,
            help=f'Dataset externo del modelo base (por defecto {RUTA_POR_DEFECTO}).',
        )

    def handle(self, *args, **options):
        categoria, origen = options['categoria'], options['origen']
        ctx = self._preparar(categoria, origen, options['archivo_externo'])

        regresor_base = xgb.XGBRegressor()
        regresor_base.load_model(ctx['base'].ruta_archivo)
        regresor_vigente = xgb.XGBRegressor()
        regresor_vigente.load_model(ctx['vigente'].ruta_archivo)
        hiperparametros = ctx['vigente'].hiperparametros or HIPERPARAMETROS_AJUSTE

        # Mismo procedimiento que evaluar_predicciones; el base "sin ajustar"
        # pronostica igual pero sin rehacer el ajuste fino en cada origen.
        # Si el vigente ya se ajustó con escalado de nivel, se evalúa con él.
        configuracion = ctx['vigente'].escalado
        vigente = ValidacionOrigenMovil(
            ctx['df'], booster_base=regresor_base.get_booster(), hiperparametros_ajuste=hiperparametros,
            con_cruda=True,
            escalado=EscaladoNivel(configuracion['nivel_minimo'], configuracion['nivel_referencia'])
            if configuracion else None,
        )
        base = ValidacionOrigenMovil(ctx['df'], ajustar=lambda _train: regresor_base, con_cruda=True)
        horizontes = {'diario': HORIZONTE_DIARIO, 'ventana': ctx['horizonte']}
        self.stdout.write(
            f'Ajuste fino por origen con la configuración del vigente (#{ctx["vigente"].pk}: {hiperparametros}) '
            f'sobre el base #{ctx["base"].pk}; el base sin ajustar se evalúa en los mismos orígenes.'
        )
        corridas = {}
        for nombre, horizonte in horizontes.items():
            try:
                ventanas, diario = vigente.evaluar(horizonte)
                ventanas_base, diario_base = base.evaluar(horizonte)
            except HistoricoInsuficiente as exc:
                raise CommandError(str(exc)) from exc
            corridas[nombre] = {
                'horizonte': horizonte, 'origenes': vigente.origenes(horizonte),
                'ventanas': ventanas, 'diario': diario,
                'ventanas_base': ventanas_base, 'diario_base': diario_base,
            }

        caracterizacion = self._caracterizar(categoria, corridas)
        hipotesis = self._hipotesis(categoria, origen, ctx, corridas, vigente, regresor_base, regresor_vigente)
        correccion = self._decidir(categoria, ctx, corridas, hipotesis, regresor_base, hiperparametros)

        ruta = self._guardar(ctx, categoria, origen, corridas, caracterizacion, hipotesis, correccion)
        self.stdout.write(f'\nReporte guardado en {ruta}.')
        self.stdout.write(self.style.WARNING(
            'No se tocaron datos ni se activó ni registró ningún modelo.'
        ))

    # --- Preparación ---------------------------------------------------------------

    def _preparar(self, categoria, origen, archivo_externo):
        vigente = ModeloEntrenado.objects.filter(
            fase=ModeloEntrenado.Fase.AJUSTADO, activo=True, nivel=NivelPrediccion.CATEGORIA,
        ).first()
        base = ModeloEntrenado.objects.filter(fase=ModeloEntrenado.Fase.BASE, activo=True).first()
        if vigente is None or base is None:
            raise CommandError(
                'Hacen falta un modelo base activo y un modelo ajustado activo a nivel de categoría.'
            )
        df = leer_demanda_interna_por_categoria(origen=origen)
        if df.empty:
            raise CommandError(f'No hay pedidos de origen "{origen}".')
        aptas, _, dias_venta = seleccionar_categorias_aptas(df, min_dias_venta=MIN_DIAS_VENTA_CATEGORIA)
        if categoria not in aptas:
            raise CommandError(
                f'La categoría "{categoria}" no tiene pronóstico del modelo (categorías evaluadas: '
                f'{", ".join(aptas) or "ninguna"}).'
            )
        df = df[df['serie_id'].isin(aptas)].reset_index(drop=True)
        try:
            horizonte, lead_times = horizonte_lead_time(aptas)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        externo = None
        if Path(archivo_externo).exists():
            externo = leer_datos_externos(archivo_externo)
        else:
            self.stdout.write(self.style.WARNING(
                f'No se encontró el dataset externo ({archivo_externo}): se omiten las mediciones que lo '
                'usan y, si correspondiera, la evaluación de la corrección.'
            ))

        self.stdout.write(self.style.SUCCESS(
            f'Diagnóstico de sesgo: categoría {categoria}, origen {origen}, '
            f'{df["fecha"].min():%d/%m/%Y}–{df["fecha"].max():%d/%m/%Y}.'
        ))
        self.stdout.write(
            f'Categorías evaluadas: {", ".join(aptas)}. Ventana de lead time: H = {horizonte} días.'
        )
        return {
            'vigente': vigente, 'base': base, 'df': df, 'aptas': aptas, 'dias_venta': dias_venta,
            'horizonte': horizonte, 'lead_times': lead_times, 'externo': externo,
        }

    # --- 1. Caracterización ---------------------------------------------------------------

    def _caracterizar(self, categoria, corridas):
        self.stdout.write(self.style.SUCCESS('\n1. Caracterización del sesgo (error = pronóstico − real)'))
        resultado = {}
        for nombre, corrida in corridas.items():
            por_ventana = diag.resumen_sesgo(corrida['ventanas'])
            por_dia = diag.resumen_sesgo(corrida['diario'])
            unidad = 'día' if nombre == 'diario' else 'ventana'
            self.stdout.write(
                f'\nHorizonte {nombre} (H = {corrida["horizonte"]}, {len(corrida["origenes"])} orígenes):'
            )
            encabezado = (
                f'{"Categoría":<12}{"Real":>9}{"Pronóst.":>10}{"Sesgo":>9}'
                f'{"Error medio/" + unidad:>22}{"Orígenes con error > 0":>25}'
            )
            self.stdout.write(encabezado)
            self.stdout.write('-' * len(encabezado))
            for cat, r in por_ventana.items():
                self.stdout.write(
                    f'{cat:<12}{r["real"]:>9.0f}{r["pronostico"]:>10.1f}{_pct(r["sesgo_relativo"], signo=True):>9}'
                    f'{_num(r["error_medio"], 2, signo=True):>22}{f"{r["positivas"]} de {r["filas"]}":>25}'
                )
            resultado[nombre] = {'por_ventana': por_ventana, 'por_dia': por_dia}

        posiciones = diag.error_por_posicion(corridas['ventana']['diario'])
        resultado['error_por_posicion'] = posiciones
        h = corridas['ventana']['horizonte']
        self.stdout.write(f'\nError medio con signo por posición dentro de la ventana (día 1 … día {h}):')
        encabezado = f'{"Categoría":<12}' + ''.join(f'{f"día {p}":>9}' for p in range(1, h + 1))
        self.stdout.write(encabezado)
        self.stdout.write('-' * len(encabezado))
        for cat, valores in posiciones.items():
            self.stdout.write(
                f'{cat:<12}' + ''.join(f'{_num(valores.get(p), 2, signo=True):>9}' for p in range(1, h + 1))
            )
        valores = posiciones[categoria]
        primero, ultimo = valores[1], valores[h]
        crece = abs(ultimo) > abs(primero) * 1.5
        self.stdout.write(
            f'En {categoria} el error del día 1 es {primero:+.2f} y el del día {h}, {ultimo:+.2f}: '
            + ('crece con la posición; el encadenamiento lo amplifica.' if crece else
               'no crece con la posición; el sesgo ya está en el primer día y no viene del encadenamiento.')
        )
        resultado['crece_con_posicion'] = crece
        return resultado

    # --- 2. Hipótesis ----------------------------------------------------------------------

    def _hipotesis(self, categoria, origen, ctx, corridas, vigente, regresor_base, regresor_vigente):
        self.stdout.write(self.style.SUCCESS('\n2. Hipótesis, en orden'))
        ventana = corridas['ventana']
        mediciones = {}

        # (a) Modelo base sin ajustar frente al vigente, mismos orígenes.
        self.stdout.write(f'\n(a) {NOMBRES_HIPOTESIS["a"]}. Sesgo del base #{ctx["base"].pk} sin ajustar '
                          f'y del vigente #{ctx["vigente"].pk}, mismos orígenes:')
        comparacion = {}
        for nombre, corrida in corridas.items():
            sesgo_base = diag.resumen_sesgo(corrida['ventanas_base'])
            sesgo_vigente = diag.resumen_sesgo(corrida['ventanas'])
            comparacion[nombre] = {'base': sesgo_base, 'vigente': sesgo_vigente}
            for cat in sesgo_vigente:
                b, v = sesgo_base[cat], sesgo_vigente[cat]
                marca = ' ←' if cat == categoria else ''
                self.stdout.write(
                    f'    {nombre:<8}{cat:<12}real {v["real"]:>7.0f}   base {b["pronostico"]:>8.1f} '
                    f'({_pct(b["sesgo_relativo"], signo=True)}, {b["positivas"]}/{b["filas"]} orígenes > 0)   '
                    f'vigente {v["pronostico"]:>8.1f} ({_pct(v["sesgo_relativo"], signo=True)}){marca}'
                )
        mediciones['a'] = {'comparacion': comparacion}

        nivel_periodo = float(vigente.serie.loc[vigente.serie['serie_id'] == categoria, 'cantidad'].mean())
        dias_pronosticados = ventana['diario'][ventana['diario']['categoria'] == categoria].drop_duplicates('fecha')
        nivel_pronosticado = float(dias_pronosticados['real'].mean())
        curva_base = diag.curva_respuesta(regresor_base, vigente.fechas, desde=vigente.min_dias_ajuste)
        curva_vigente = diag.curva_respuesta(regresor_vigente, vigente.fechas, desde=vigente.min_dias_ajuste)
        mediciones['a'].update(
            nivel_periodo=nivel_periodo, nivel_dias_pronosticados=nivel_pronosticado,
            curva_respuesta_base=curva_base, curva_respuesta_vigente=curva_vigente,
        )
        self.stdout.write(
            '  Curva de respuesta (pronóstico a un paso con historial constante a cada nivel, '
            'fechas del periodo):'
        )
        self.stdout.write('    Nivel del historial  ' + ''.join(f'{n:>7}' for n in curva_base))
        self.stdout.write('    Base                 ' + ''.join(f'{v:>7.1f}' for v in curva_base.values()))
        self.stdout.write('    Vigente              ' + ''.join(f'{v:>7.1f}' for v in curva_vigente.values()))
        self.stdout.write(
            f'  Nivel de {categoria}: {nivel_periodo:.1f} u/día en el periodo y {nivel_pronosticado:.1f} u/día '
            'en los días pronosticados.'
        )
        if ctx['externo'] is not None:
            externo = diag.niveles_serie_externa(ctx['externo'], nivel_periodo)
            mediciones['a']['dataset_externo'] = externo
            self.stdout.write(
                f'  Dataset externo: media diaria por serie entre {externo["media_serie_minima"]:.1f} y '
                f'{externo["media_serie_maxima"]:.1f} (mediana {externo["media_serie_mediana"]:.1f}); solo el '
                f'{_pct(externo["fraccion_dias_hasta_nivel"], 2)} de sus días tiene demanda ≤ '
                f'{nivel_periodo:.1f}.'
            )

        # (b) Piso en cero.
        recortes = diag.conteo_recortes(ventana['diario'])
        mediciones['b'] = recortes
        self.stdout.write(f'\n(b) {NOMBRES_HIPOTESIS["b"]}. Predicciones del vigente (ventana) recortadas a 0:')
        for cat, r in recortes.items():
            self.stdout.write(
                f'    {cat:<12}{r["recortadas"]} de {r["predicciones"]} (suman {r["suma_recortada"]:.1f} u); '
                f'sesgo con recorte {_pct(r["sesgo_con_recorte"], signo=True)}, '
                f'sin recorte {_pct(r["sesgo_sin_recorte"], signo=True)}'
            )

        # (c) Días en cero; la media móvil de 7 días se construye con las
        # mismas medias móviles que el modelo.
        ceros = diag.proporcion_dias_cero(vigente.serie)
        sesgo_mm = diag.resumen_sesgo(ventana['ventanas'], 'media_movil_7')
        mediciones['c'] = {
            'proporcion_dias_cero': ceros,
            'sesgo_media_movil_7': {c: r['sesgo_relativo'] for c, r in sesgo_mm.items()},
        }
        self.stdout.write(f'\n(c) {NOMBRES_HIPOTESIS["c"]}. Días con demanda cero en el periodo y sesgo de la '
                          'línea base de media móvil 7 días (ventana):')
        for cat, proporcion in ceros.items():
            self.stdout.write(
                f'    {cat:<12}{_pct(proporcion):>7} de días en cero; media móvil 7: '
                f'{_pct(sesgo_mm[cat]["sesgo_relativo"], signo=True)}'
            )

        # (d) Productos de la categoría.
        distribucion = diag.distribucion_productos(categoria, origen)
        resumen = diag.resumen_productos(distribucion)
        mediciones['d'] = {'resumen': resumen, 'productos': distribucion}
        self.stdout.write(
            f'\n(d) {NOMBRES_HIPOTESIS["d"]}. {resumen["productos"]} productos activos; '
            f'{resumen["con_pedidos"]} con pedidos y {resumen["sin_pedidos"]} sin ninguno. Los 3 mayores '
            f'concentran el {_pct(resumen["participacion_top3"])}; ningún producto tiene pedidos en más de '
            f'{resumen["dias_con_pedido_maximo"]} día(s) (mediana {resumen["dias_con_pedido_mediana"]:g}).'
        )
        for fila in distribucion:
            if fila['unidades'] > 0:
                self.stdout.write(
                    f'    {fila["codigo"]:<10}{fila["nombre"][:28]:<30}{fila["unidades"]:>6.0f} u  '
                    f'{fila["dias_con_pedido"]} día(s)  {_pct(fila["participacion"])}'
                )

        veredicto = diag.identificar_causa(
            categoria,
            sesgo_vigente=comparacion['ventana']['vigente'][categoria]['sesgo_relativo'],
            sesgo_base=comparacion['ventana']['base'][categoria]['sesgo_relativo'],
            recortes=recortes[categoria],
            ceros=ceros,
            sesgo_media_movil=sesgo_mm[categoria]['sesgo_relativo'],
        )
        mediciones['veredicto'] = veredicto
        self._imprimir_veredicto(categoria, veredicto)
        return mediciones

    def _imprimir_veredicto(self, categoria, veredicto):
        h = veredicto['hipotesis']
        self.stdout.write(self.style.SUCCESS('\nIdentificación (criterios fijos, ventana de lead time):'))
        self.stdout.write(
            f'  (a) {"IDENTIFICADA" if h["a"]["identificada"] else "descartada"}: el base sin ajustar da cuenta '
            f'del {_pct(h["a"]["fraccion_explicada"], 0)} del sesgo del vigente '
            f'(criterio: ≥ {diag.FRACCION_CAUSA:.0%}, mismo signo).'
        )
        self.stdout.write(
            f'  (b) {"IDENTIFICADA" if h["b"]["identificada"] else "descartada"}: el recorte da cuenta del '
            f'{_pct(h["b"]["fraccion_explicada"], 0)} del exceso (criterio: ≥ {diag.FRACCION_CAUSA:.0%}).'
        )
        self.stdout.write(
            f'  (c) {"IDENTIFICADA" if h["c"]["identificada"] else "descartada"}: '
            f'{"muchos" if h["c"]["muchos_ceros"] else "no muchos"} más días en cero que las demás '
            f'(criterio: ≥ {diag.MULTIPLO_CEROS:g}×); la media móvil de 7 días '
            f'{"sí" if h["c"]["media_movil_mismo_signo"] else "no"} tiene sesgo del mismo signo '
            '(las variables móviles no llevan el sesgo si ella no lo tiene).'
        )
        self.stdout.write(
            '  (d) no contrastable como causa: el modelo pronostica la categoría y nunca ve productos; '
            'se reporta como descripción y limitación de la agrupación.'
        )
        dominante = veredicto['dominante']
        if dominante:
            self.stdout.write(self.style.SUCCESS(
                f'Causa dominante en {categoria}: ({dominante}) {NOMBRES_HIPOTESIS[dominante]}.'
            ))
        else:
            self.stdout.write(self.style.WARNING(f'Ninguna hipótesis queda identificada en {categoria}.'))

    # --- 3. Corrección condicionada ---------------------------------------------------------

    def _decidir(self, categoria, ctx, corridas, hipotesis, regresor_base, hiperparametros):
        self.stdout.write(self.style.SUCCESS('\n3. Corrección'))
        dominante = hipotesis['veredicto']['dominante']
        if dominante is None:
            self.stdout.write(
                'Sin causa identificada no se corrige nada. Para decidir haría falta más histórico '
                '(más orígenes) o una hipótesis nueva con su propia medición.'
            )
            return None
        if dominante == 'b':
            self.stdout.write(
                'Se deja el piso en cero (una demanda negativa no tiene sentido); el sesgo que introduce '
                'queda reportado como limitación.'
            )
            return None
        if dominante == 'c':
            self.stdout.write(
                f'Correspondería tratar {categoria} solo por punto de reorden, como las categorías con menos '
                f'de {MIN_DIAS_VENTA_CATEGORIA} días con venta. No se aplica desde este comando: es un cambio '
                'del sistema que decide el usuario.'
            )
            return None
        if ctx['vigente'].escalado:
            self.stdout.write(
                f'El vigente #{ctx["vigente"].pk} ya se ajustó con escalado de nivel (la corrección de (a)); '
                'las cifras de arriba ya lo incluyen.'
            )
            return None
        if ctx['externo'] is None:
            self.stdout.write(self.style.WARNING(
                'La corrección de (a) necesita el dataset externo para fijar los niveles; no se evalúa.'
            ))
            return None
        return self._evaluar_escalado(categoria, ctx, corridas, regresor_base, hiperparametros)

    def _evaluar_escalado(self, categoria, ctx, corridas, regresor_base, hiperparametros):
        niveles = niveles_externos(ctx['externo'])
        self.stdout.write(
            'Corrección de (a): escalar antes del ajuste fino cada serie cuyo nivel medio en los días de '
            f'ajuste quede por debajo de la serie externa de menor nivel ({niveles["minimo"]:.2f} u/día), '
            f'llevándola a la mediana externa ({niveles["referencia"]:.2f} u/día); ajustar y pronosticar en '
            'esa escala y deshacerla en el pronóstico. Las demás series no se tocan. Niveles fijados antes '
            'de evaluar. Esto CAMBIA el procedimiento de entrenamiento; no se aplica a producción.'
        )
        escalado = ValidacionOrigenMovil(
            ctx['df'], booster_base=regresor_base.get_booster(), hiperparametros_ajuste=hiperparametros,
            escalado=EscaladoNivel(niveles['minimo'], niveles['referencia']),
        )
        resultado = {'niveles': niveles, 'horizontes': {}}
        for nombre, corrida in corridas.items():
            ventanas_despues, diario_despues = escalado.evaluar(corrida['horizonte'])
            antes, despues = resumir(corrida['ventanas']), resumir(ventanas_despues)
            resultado['horizontes'][nombre] = {
                'antes': antes, 'despues': despues,
                'sesgo_antes': diag.resumen_sesgo(corrida['ventanas']),
                'sesgo_despues': diag.resumen_sesgo(ventanas_despues),
                'ventanas_despues': ventanas_despues, 'diario_despues': diario_despues,
            }
            resultado['horizontes'][nombre]['origenes_que_mejoran'] = diag.comparar_por_origen(
                corrida['ventanas'], ventanas_despues,
            )
            self._imprimir_antes_despues(nombre, corrida['horizonte'], resultado['horizontes'][nombre])

        factores = {
            str(serie_id): sorted({round(f[serie_id], 3) for f in escalado.factores.values()})
            for serie_id in ctx['aptas']
        }
        resultado['factores_por_serie'] = factores
        for serie_id, valores in factores.items():
            rango = 'sin escalar' if valores == [1.0] else f'factor {min(valores):.2f}–{max(valores):.2f}'
            self.stdout.write(f'  {serie_id}: {rango} según el origen.')
        return resultado

    def _imprimir_antes_despues(self, nombre, horizonte, r):
        self.stdout.write(f'\nHorizonte {nombre} (H = {horizonte}), antes → después de escalar:')
        lineas = [(c, n) for c, (n, _) in LINEAS_BASE.items()]
        encabezado = (
            f'{"Categoría":<19}{"Real":>8}{"Pronóst.":>18}{"Sesgo":>18}{"WAPE":>18}'
            + ''.join(f'{"WAPE " + c:>22}' for c, _ in lineas)
        )
        self.stdout.write(encabezado)
        self.stdout.write('-' * len(encabezado))
        antes, despues = r['antes'], r['despues']
        filas = list(antes['modelo']['categorias']) + ['Total']
        for cat in filas:
            a = antes['modelo']['total'] if cat == 'Total' else antes['modelo']['categorias'][cat]
            d = despues['modelo']['total'] if cat == 'Total' else despues['modelo']['categorias'][cat]
            sesgo_a = (a['demanda_pronosticada'] - a['demanda_real']) / a['demanda_real'] if a['demanda_real'] else None
            sesgo_d = (d['demanda_pronosticada'] - d['demanda_real']) / d['demanda_real'] if d['demanda_real'] else None
            bases = ''.join(
                f'{_pct((antes[c]["total"] if cat == "Total" else antes[c]["categorias"][cat])["wape"]):>22}'
                for c, _ in lineas
            )
            self.stdout.write(
                f'{cat:<19}{a["demanda_real"]:>8.0f}'
                f'{f"{a["demanda_pronosticada"]:.1f} → {d["demanda_pronosticada"]:.1f}":>18}'
                f'{f"{_pct(sesgo_a, signo=True)} → {_pct(sesgo_d, signo=True)}":>18}'
                f'{f"{_pct(a["wape"])} → {_pct(d["wape"])}":>18}{bases}'
            )
        pareado = r['origenes_que_mejoran']
        detalle = ', '.join(
            f'{c} {v["mejoran"]}/{v["origenes"]}' for c, v in pareado['por_categoria'].items()
        )
        self.stdout.write(
            f'Comparación pareada por origen: el WAPE total baja en {pareado["total"]["mejoran"]} de '
            f'{pareado["total"]["origenes"]} orígenes y sube en {pareado["total"]["empeoran"]} '
            f'(por categoría, orígenes en que baja: {detalle}).'
        )
        no_supera = [LINEAS_BASE[c][0] for c, supera in despues['supera'].items() if not supera]
        if no_supera:
            self.stdout.write(self.style.WARNING(
                f'Después de escalar, el modelo NO supera en WAPE total a: {", ".join(no_supera)}.'
            ))
        else:
            self.stdout.write(self.style.SUCCESS('Después de escalar, el modelo supera en WAPE a las tres líneas base.'))

    # --- Reporte --------------------------------------------------------------------------

    def _guardar(self, ctx, categoria, origen, corridas, caracterizacion, hipotesis, correccion):
        DIR_REPORTES.mkdir(parents=True, exist_ok=True)
        ahora = timezone.now()
        ruta = DIR_REPORTES / f'diagnostico_sesgo_{categoria}_{ahora:%Y%m%d_%H%M%S}.json'

        def registros(df):
            df = df.copy()
            for columna in ('origen', 'fecha', 'fecha_inicio', 'fecha_fin'):
                if columna in df:
                    df[columna] = pd.to_datetime(df[columna]).dt.strftime('%Y-%m-%d')
            return df.to_dict(orient='records')

        if correccion is not None:
            for seccion in correccion['horizontes'].values():
                seccion['ventanas_despues'] = registros(seccion['ventanas_despues'])
                seccion['diario_despues'] = registros(seccion['diario_despues'])
                for clave in ('antes', 'despues'):
                    seccion[clave] = {m: seccion[clave][m] for m in [*METODOS, 'dispersion', 'supera']}

        reporte = {
            'fecha': ahora.isoformat(),
            'categoria': categoria,
            'origen': origen,
            'modelo_ajustado': ctx['vigente'].pk,
            'modelo_base': ctx['base'].pk,
            'categorias_evaluadas': ctx['aptas'],
            'lead_times_productos': ctx['lead_times'],
            'criterios': {'fraccion_causa': diag.FRACCION_CAUSA, 'multiplo_ceros': diag.MULTIPLO_CEROS},
            'horizontes': {
                nombre: {
                    'horizonte_dias': c['horizonte'],
                    'origenes': [pd.Timestamp(o).strftime('%Y-%m-%d') for o in c['origenes']],
                    'pronosticos_diarios': registros(c['diario']),
                    'pronosticos_diarios_base_sin_ajustar': registros(c['diario_base']),
                }
                for nombre, c in corridas.items()
            },
            'caracterizacion': caracterizacion,
            'hipotesis': hipotesis,
            'correccion': correccion,
        }
        ruta.write_text(json.dumps(reporte, ensure_ascii=False, indent=2, default=str))
        return ruta
