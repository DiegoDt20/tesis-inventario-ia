"""Estudio de ablación: cuánto aporta cada fuente de datos al pronóstico
(comando evaluar_ablacion).

Configuraciones, todas sobre la MISMA validación de origen móvil que
evaluar_predicciones (mismos orígenes, mismas filas, mismos horizontes):

  A           Solo externo: el modelo base (Kaggle) tal cual, sin ajuste con
              datos de la empresa. Recibe como ENTRADA las variables
              construidas con el historial de agosto hasta cada origen: una
              serie no se puede pronosticar sin su propio historial, así que
              "sin datos de la empresa" no es posible en sentido estricto; la
              configuración es "sin ajuste con datos de la empresa".
  A_escalado  Igual que A, con el escalado de nivel del vigente (misma regla
              y niveles; factores con los días <= origen): mide el piso del
              base sobre datos reales.
  B           Solo interno: XGBoost entrenado desde cero solo con los días
              de agosto hasta cada origen, sin el modelo base, con los
              MISMOS hiperparámetros que el ajuste fino del vigente (la única
              diferencia con C es la ausencia de transferencia). Sin
              escalado: no hay dataset externo cuyo nivel igualar.
  C           Externo + interno: el procedimiento del vigente (#6): ajuste
              fino del base con agosto hasta cada origen, con escalado.

Más las tres líneas base de siempre. Estas decisiones (qué configuración
lleva escalado y con qué hiperparámetros) se fijaron antes de correr y no se
cambian según el resultado.

Criterio de "funciona" (el mismo de la evaluación de exactitud, fijado en la
tarea de la ventana de lead time): en el horizonte de ventana, el WAPE total
de la configuración es menor que el de las tres líneas base.
"""
from .diagnostico_sesgo import resumen_sesgo
from .entrenamiento import entrenar_modelo
from .escalado import EscaladoNivel
from .evaluacion import LINEAS_BASE, evaluar_exactitud
from .walk_forward import ValidacionOrigenMovil, agregar_por_ventana, comparar_por_origen, dispersion_entre_origenes

CONFIGURACIONES = {
    'A': 'A. Solo externo (sin ajuste con datos de la empresa)',
    'A_escalado': 'A. Solo externo, con escalado de nivel',
    'B': 'B. Solo interno (sin transferencia)',
    'C': 'C. Externo + interno (vigente)',
}

COLUMNAS = [*CONFIGURACIONES, *LINEAS_BASE]

# Pares de la comparación origen por origen: (configuración, contra, qué mide).
PARES = [
    ('C', 'A', 'aporte del ajuste con datos de la empresa (y del escalado)'),
    ('C', 'A_escalado', 'aporte del ajuste con datos de la empresa, ambos con escalado'),
    ('C', 'B', 'aporte de la transferencia desde Kaggle'),
    ('A', 'B', 'cuál fuente sola rinde mejor'),
    ('A_escalado', 'A', 'efecto del escalado sobre el base sin ajuste'),
]


def construir_validaciones(df_interno, regresor_base, hiperparametros_ajuste, nivel_minimo, nivel_referencia,
                           entrenar=entrenar_modelo):
    """Una ValidacionOrigenMovil por configuración, todas sobre el mismo
    df_interno. `regresor_base` es el base ya cargado: A lo usa tal cual (su
    "ajuste" devuelve el base sin tocarlo) y C continúa su entrenamiento. B
    entrena desde cero, sin recibir el base."""
    escalado = EscaladoNivel(nivel_minimo, nivel_referencia)
    return {
        'A': ValidacionOrigenMovil(df_interno, ajustar=lambda _train: regresor_base),
        'A_escalado': ValidacionOrigenMovil(df_interno, ajustar=lambda _train: regresor_base, escalado=escalado),
        'B': ValidacionOrigenMovil(df_interno, ajustar=lambda train: entrenar(train, hiperparametros_ajuste)),
        'C': ValidacionOrigenMovil(
            df_interno, booster_base=regresor_base.get_booster(), hiperparametros_ajuste=hiperparametros_ajuste,
            escalado=escalado,
        ),
    }


def unir_configuraciones(diarios):
    """Une el pronóstico diario de cada configuración ({clave: diario de
    ValidacionOrigenMovil.evaluar}) en un solo DataFrame: origen, fecha,
    categoria, real, una columna por configuración y las líneas base (de C;
    no dependen del modelo). Lanza ValueError si las configuraciones no
    cubren exactamente las mismas filas con la misma demanda real."""
    llaves = ['origen', 'fecha', 'categoria', 'real']
    referencia = diarios['C'][llaves].sort_values(llaves).reset_index(drop=True)
    unido = diarios['C'][[*llaves, *LINEAS_BASE]]
    for clave, diario in diarios.items():
        filas = diario[llaves].sort_values(llaves).reset_index(drop=True)
        if not filas.equals(referencia):
            raise ValueError(f'La configuración {clave} no se evaluó sobre las mismas filas que C.')
        unido = unido.merge(
            diario[[*llaves, 'modelo']].rename(columns={'modelo': clave}), on=llaves, how='inner',
            validate='one_to_one',
        )
    return unido[[*llaves, *COLUMNAS]]


def resumir_ablacion(filas, tolerancia_relativa=None):
    """Métricas de cada columna (configuración o línea base) sobre `filas`
    (días o ventanas): exactitud (evaluar_exactitud), sesgo con signo por
    categoría y dispersión del WAPE entre orígenes; más la comparación
    origen por origen de cada par de PARES y de cada configuración contra
    la media por categoría."""
    resultado = {
        clave: {
            'exactitud': evaluar_exactitud(filas['real'], filas[clave], filas['categoria'], tolerancia_relativa),
            'sesgo': resumen_sesgo(filas, clave),
            'dispersion': dispersion_entre_origenes(filas, clave),
        }
        for clave in COLUMNAS
    }
    dispersion = {clave: resultado[clave]['dispersion'] for clave in COLUMNAS}
    comparaciones = [
        {'configuracion': a, 'contra': b, 'mide': mide, **comparar_por_origen(dispersion, b, a)}
        for a, b, mide in PARES
    ]
    comparaciones += [
        {'configuracion': clave, 'contra': base, 'mide': f'{clave} contra la línea base',
         **comparar_por_origen(dispersion, base, clave)}
        for clave in CONFIGURACIONES for base in LINEAS_BASE
    ]
    return {'metodos': resultado, 'comparaciones': comparaciones}


def wape_total(resumen, clave):
    return resumen['metodos'][clave]['exactitud']['total']['wape']


def supera_lineas_base(resumen, clave):
    """True si el WAPE total de `clave` es menor que el de las tres líneas
    base (el criterio de "funciona")."""
    wape = wape_total(resumen, clave)
    return wape is not None and all(
        wape_total(resumen, base) is not None and wape < wape_total(resumen, base) for base in LINEAS_BASE
    )


def comparacion(resumen, configuracion, contra):
    return next(
        c for c in resumen['comparaciones'] if c['configuracion'] == configuracion and c['contra'] == contra
    )


def evaluar(validaciones, horizonte):
    """Corre todas las configuraciones con ese horizonte y devuelve
    (diario unido, ventanas)."""
    diarios = {clave: validacion.evaluar(horizonte)[1] for clave, validacion in validaciones.items()}
    diario = unir_configuraciones(diarios)
    return diario, agregar_por_ventana(diario, COLUMNAS)

