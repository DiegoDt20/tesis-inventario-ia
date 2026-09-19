"""Fórmulas clásicas de gestión de inventario para el motor de decisiones
de reposición.

Todo este módulo es lógica determinística: ninguna función de aquí usa
aprendizaje automático. La demanda predicha (que sí sale de un modelo de
ML) y la demanda histórica real son simplemente los números de entrada.
"""
from scipy.stats import norm


def stock_seguridad(desviacion_demanda, lead_time, nivel_servicio_objetivo):
    """Stock de seguridad (SS), modelo clásico de demanda variable con
    lead time constante:

        SS = z * sigma_d * sqrt(L)

    - z: percentil de la normal estándar para el nivel de servicio
      objetivo (P(Z <= z) = nivel_servicio_objetivo), vía scipy.stats.norm.ppf.
    - sigma_d: desviación estándar de la demanda diaria.
    - L: lead time en días.

    Fuente: Silver, E. A., Pyke, D. F., & Peterson, R. (1998).
    "Inventory Management and Production Planning and Scheduling" (3.ª ed.),
    fórmula del stock de seguridad para demanda estocástica con lead time
    fijo. También en Chopra, S., & Meindl, P., "Supply Chain Management",
    cap. 12 (política de revisión continua (r, Q)).
    """
    z = norm.ppf(nivel_servicio_objetivo)
    return z * desviacion_demanda * (lead_time ** 0.5)


def punto_reorden(demanda_diaria_esperada, lead_time, stock_seguridad):
    """Punto de reorden (ROP):

        ROP = d_promedio * L + SS

    - d_promedio: demanda diaria esperada.
    - L: lead time en días.
    - SS: stock de seguridad.

    Es el nivel de inventario en el que hay que lanzar un nuevo pedido para
    no quedarse sin stock durante el lead time del proveedor.

    Fuente: Silver, Pyke & Peterson (1998), íd.; Chopra & Meindl,
    "Supply Chain Management", cap. 12.
    """
    return demanda_diaria_esperada * lead_time + stock_seguridad


def cantidad_a_pedir(demanda_periodo, stock_actual, stock_seguridad, pedidos_en_transito=0):
    """Cantidad a pedir (política de revisión periódica "order-up-to
    level"): lo necesario para cubrir la demanda del periodo de cobertura
    más el stock de seguridad, descontando lo que ya hay en almacén y lo
    que ya viene en camino:

        Q = max(0, demanda_periodo + SS - stock_actual - pedidos_en_transito)

    Nunca es negativa: si ya hay más que suficiente cubierto (en almacén o
    en tránsito), no tiene sentido pedir un número negativo, simplemente no
    se sugiere pedir nada.

    Fuente: Silver, Pyke & Peterson (1998), cap. 6 (política "order-up-to").
    """
    cantidad = demanda_periodo + stock_seguridad - stock_actual - pedidos_en_transito
    return max(cantidad, 0.0)
