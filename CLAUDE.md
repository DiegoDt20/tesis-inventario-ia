# Contexto del proyecto

Sistema web con inteligencia artificial para la gestión de inventario en una
microempresa del sector pintura en Lima. Es el sistema desarrollado para una
tesis de Ingeniería de Sistemas.

## Diseño de investigación

Preexperimental, con medición pretest y postest sobre el mismo grupo (la
microempresa), antes y después de implementar el sistema. El sistema debe
permitir registrar los datos necesarios para calcular, en ambos momentos, tres
indicadores.

## Indicadores

- **Exactitud del inventario (EI)**
  `EI = (stock registrado correctamente / total de registros) × 100`
  Se apoya en `ConteoFisico` y `ConteoDetalle` (comparación stock_sistema vs
  stock_fisico; diferencia = 0 significa registro correcto).

- **Nivel de servicio (NS)**
  `NS = (pedidos atendidos a tiempo / pedidos totales) × 100`
  Se apoya en `Pedido` y `PedidoDetalle` (campo `atendido_a_tiempo`).

- **Costos operativos de inventario (COI)**
  `COI = costos de almacenamiento + pérdidas por desabastecimiento`
  Se apoya en `CostoAlmacenamiento` (costos de almacenamiento) y `Merma`
  combinada con pedidos no atendidos por falta de stock (pérdidas por
  desabastecimiento).

## Stack

- Python + Django 5.2 (LTS)
- PostgreSQL (base de datos: `inventario_ia`, usuario: `diego`)
- Configuración vía `.env` con `python-decouple` (nunca credenciales en
  `settings.py` ni en el repositorio; `.env` está en `.gitignore`, usar
  `.env.example` como plantilla)
- Todos los montos monetarios son `DecimalField(max_digits=12,
  decimal_places=2)`, nunca `FloatField`, para evitar errores de redondeo en
  los cálculos de costos.

## Estado actual

### Etapa 1 — modelo de datos e importación (completa)

- Proyecto Django `core`, app `inventario`.
- Modelos: `Producto`, `Proveedor`, `Compra`, `Movimiento`, `Pedido`,
  `PedidoDetalle`, `ConteoFisico`, `ConteoDetalle`, `Merma`,
  `CostoAlmacenamiento`.
- Todos los modelos registrados en `admin.py` con `list_display`,
  `list_filter` y `search_fields`.
- Campo `origen` (`Origen.PRUEBA` / `Origen.REAL`) en `Producto`,
  `Movimiento`, `Pedido`, `ConteoFisico`, `Merma` y `CostoAlmacenamiento`,
  para poder separar y limpiar conjuntos de datos de prueba (pretest/postest)
  de datos reales.
- Comando `cargar_datos --archivo ruta.xlsx --origen prueba|real
  [--limpiar] [--dry-run]` (`inventario/management/commands/cargar_datos.py`):
  importa las fichas de registro (Excel con hojas `1_EI`, `2_NS`, `3_COI_CA`,
  `3_COI_PD`) y calcula los tres indicadores desde la base de datos para
  verificar que la importación no deformó los datos.

### Etapa 2 — motor de predicción de demanda con transferencia de aprendizaje (en curso)

Preentrenamiento con un dataset externo (indicación del asesor de tesis) y
ajuste posterior con los datos reales/de prueba de la microempresa.

- **Regla que manda sobre todo**: el conjunto de variables debe ser
  IDÉNTICO entre preentrenamiento y ajuste. El dataset externo solo tiene
  fecha, producto (tienda+ítem) y cantidad vendida, así que **no se usan
  variables que ese dataset no tenga** (nada de precio, stock, etc.); todas
  las variables se derivan únicamente de fecha/serie/cantidad.
- Dataset externo: `datos/train.csv` (Store Item Demand Forecasting
  Challenge, Kaggle; 913,000 filas, `date,store,item,sales`, 2013-2017, 10
  tiendas × 50 productos). No se versiona (`datos/` en `.gitignore`).
- Módulo `inventario/ml/`:
  - `features.py` — `construir_features(df)` (recibe `fecha`, `serie_id`,
    `cantidad`; misma función para ambas fuentes) y `dividir_temporal(df,
    dias_test)`. Densifica cada serie a diario (días sin ventas = cantidad
    cero, nunca se omiten) y deriva día de semana/mes/año, fin de semana,
    rezagos de 7/14/30 días y medias/desviación móviles de 7/30, todas
    calculadas solo con información pasada (sin fuga temporal).
  - `carga_externa.py` — lee `datos/train.csv`, `serie_id = store-item`.
  - `carga_interna.py` — lee la demanda desde `PedidoDetalle
    .cantidad_solicitada` (no `Movimiento`, no `cantidad_atendida`: la
    demanda real es lo que el cliente pidió, no lo que se le pudo entregar).
  - `entrenamiento.py` — Fase 1 (`entrenar_fase_base`): `XGBRegressor` desde
    cero sobre el dataset externo. Fase 2 (`entrenar_fase_ajuste`): continúa
    el entrenamiento sobre datos internos vía `xgb_model=`, con menos
    árboles y learning rate más bajo. División train/test siempre TEMPORAL
    (últimos N días como test); nunca `train_test_split` con shuffle.
  - `evaluacion.py` — MAE, RMSE, MAPE, R²; compara línea base ingenua,
    modelo solo-interno y modelo preentrenado+ajustado sobre el mismo test
    interno, para demostrar si la transferencia aporta valor.
- Modelos `ModeloEntrenado` (métricas, hiperparámetros, fase, archivo,
  `activo`) y `Prediccion` (producto, fecha objetivo, demanda predicha/real,
  modelo usado).
- Comandos: `entrenar_base`, `entrenar_ajustado`, `predecir_demanda --dias
  N`, `evaluar_predicciones`.
- Los archivos de modelo entrenado (`.json` de XGBoost) se guardan en
  `modelos/`, que no se versiona (`.gitignore`).
- Dependencias añadidas: `pandas`, `numpy`, `scikit-learn`, `xgboost`.

Explícitamente **fuera de alcance** por ahora: chat/IA conversacional y
frontend. Deben apoyarse en este mismo modelo de datos (y en el motor de
predicción, cuando aplique) salvo que el usuario indique lo contrario.

## Convenciones a mantener

- Idioma de nombres de modelos, campos y textos de la interfaz: español (es
  el idioma de la tesis y del negocio real).
- Índices (`db_index=True` o `Meta.indexes`) en campos de fecha y en FKs que
  se usarán para filtrar por periodo (necesario para los reportes de los tres
  indicadores).
- `LANGUAGE_CODE = 'es-pe'`, `TIME_ZONE = 'America/Lima'`.

## Convenciones de código

- Todos los comentarios y docstrings se escriben en español.
- Los mensajes de salida de comandos y los textos de error van en español.
- Los nombres de campos de modelos van en español (ya establecido).
- Los mensajes de commit van en español.
