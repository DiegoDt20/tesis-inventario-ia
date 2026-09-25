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
  Se apoya en `CostoAlmacenamiento` (costos de almacenamiento) y en los
  `PedidoDetalle` no atendidos por completo, valorizados al margen
  (`precio_venta - costo_compra`) del producto (pérdidas por
  desabastecimiento). `calcular_coi()` (`inventario/servicios/indicadores.py`)
  **no** lee el modelo `Merma` directamente. Las mermas ya entran al COI por
  otra vía: `cargar_datos` (hoja `3_COI_CA`) carga el monto agregado
  "Mermas del periodo" como una fila más de `CostoAlmacenamiento`, porque
  esa hoja no trae el desglose por producto/cantidad que `Merma` exige. Si
  algún día se quiere registrar mermas individuales en `Merma` y sumarlas al
  COI, hay que dejar de cargarlas también como fila de
  `CostoAlmacenamiento` (o restarlas de ahí): sumar ambas fuentes sin
  ajustar ninguna cuenta la misma merma dos veces.

## Stack

- Python + Django 5.2 (LTS)
- PostgreSQL (base de datos: `inventario_ia`, usuario: `diego`)
- Configuración vía `.env` con `python-decouple` (nunca credenciales en
  `settings.py` ni en el repositorio; `.env` está en `.gitignore`, usar
  `.env.example` como plantilla)
- Todos los montos monetarios son `DecimalField(max_digits=12,
  decimal_places=2)`, nunca `FloatField`, para evitar errores de redondeo en
  los cálculos de costos.

## Estructura del proyecto

- `inventario/models/` — paquete de modelos, dividido por dominio:
  `operacion.py` (catálogo, compras, movimientos, pedidos, conteos),
  `costos.py` (mermas y costos de almacenamiento) e `ia.py` (motor de
  predicción, motor de decisiones, anomalías, asistente conversacional).
  Todo se reexporta desde `inventario/models/__init__.py`, así que
  `from inventario.models import X` sigue funcionando igual sin importar en
  qué submódulo viva `X`.
- `inventario/views/` — paquete de vistas, un archivo por pantalla:
  `dashboard.py`, `pedidos.py`, `movimientos.py`, `conteos.py`,
  `recomendaciones.py`, `anomalias.py`, `asistente.py`, más `_comunes.py`
  con los parseos de querystring compartidos. `urls.py` importa cada
  submódulo directamente (`from .views import dashboard, pedidos, ...`).
- `inventario/servicios/indicadores.py` — cálculo de EI/NS/COI (antes
  `inventario/indicadores.py`).
- `inventario/tests/` — paquete de tests, un archivo `test_*.py` por área
  (p. ej. `test_carga_datos.py`, `test_ml.py`, `test_vistas.py`).
- `artefactos/modelos_ml/` — modelos de XGBoost entrenados (`.json`), no se
  versiona (`artefactos/` en `.gitignore`). Antes era `modelos/`.
- `inventario/ml/`, `inventario/decisiones/`, `inventario/asistente/` y
  `inventario/management/commands/` no cambiaron de ubicación.

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
- Comando `cargar_precios --archivo precios.xlsx [--hoja nombre] [--dry-run]`
  (`inventario/management/commands/cargar_precios.py`): actualiza
  `precio_venta`, `costo_compra` y `stock_minimo` de productos existentes
  (columnas código, precio de venta, costo de compra, stock mínimo). No crea
  productos; reporta actualizados, códigos inexistentes y productos activos
  que siguen sin precio (precio de venta o costo de compra en 0).

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
    (últimos N días como test, `--dias-test`); nunca `train_test_split` con
    shuffle. El comando `entrenar_ajustado` rechaza una combinación de
    histórico disponible y `--dias-test` que deje menos de
    `MIN_DIAS_ENTRENAMIENTO` (14) días para entrenar — con poco histórico
    interno (agosto 2026: 31 días), un `--dias-test` grande (el valor por
    defecto es 30) deja casi todo el histórico como test y casi nada para
    entrenar, lo que invalida la comparación de los tres modelos.
  - `evaluacion.py` — MAE, RMSE, MAPE, R²; compara línea base ingenua,
    modelo solo-interno y modelo preentrenado+ajustado sobre el mismo test
    interno, para demostrar si la transferencia aporta valor.
- Modelos `ModeloEntrenado` (métricas, hiperparámetros, fase, archivo,
  `activo`, y para la fase "ajustado" también `origen_datos_internos`,
  `dias_entrenamiento` y `dias_prueba`) y `Prediccion` (producto, fecha
  objetivo, demanda predicha/real, modelo usado).
- Comandos: `entrenar_base`, `entrenar_ajustado`, `predecir_demanda --dias
  N`, `evaluar_predicciones`.
- Los archivos de modelo entrenado (`.json` de XGBoost) se guardan en
  `artefactos/modelos_ml/`, que no se versiona (`.gitignore`).
- Dependencias añadidas: `pandas`, `numpy`, `scikit-learn`, `xgboost`.

### Etapa 3 — motor de decisiones, detección de anomalías y asistente conversacional (implementados)

Ya no están fuera de alcance: los tres están implementados y en uso.

- **Motor de decisiones** (`inventario/decisiones/`) — determinístico, no
  IA: `calculos.py` (stock de seguridad, punto de reorden, cantidad a
  pedir) y `motor.py` (arma la `Recomendacion` con la explicación en texto
  de `explicacion.py`). Comando `generar_recomendaciones`. Cada
  `Recomendacion` guarda también los datos de entrada para auditar el
  cálculo en pantalla (horizonte, demanda de la categoría, participación
  aplicada, origen del lead time, pedidos en tránsito); el costo estimado
  y los días de cobertura se calculan al mostrarla, con el costo de compra
  actual del producto.
- **Detección de anomalías** (`inventario/ml/anomalias.py`) — Isolation
  Forest + regla del 20% sobre diferencias de inventario
  (`ConteoDetalle`), y z-score sobre el histórico propio de cada producto
  para movimientos atípicos (mínimo `MIN_MOVIMIENTOS_ZSCORE = 5`
  movimientos previos). Guarda registros `Anomalia`. Comando
  `detectar_anomalias`: vuelve a correrse sin duplicar (actualiza la
  anomalía del mismo `ConteoDetalle`/`Movimiento` y conserva su revisión).
  Severidad de diferencias de inventario según % sobre el stock de sistema,
  configurable en `.env`: alta ≥ `ANOMALIA_UMBRAL_ALTA` (1.0; inclusivo
  para que un faltante total, físico en 0, sea alta), media desde
  `ANOMALIA_UMBRAL_MEDIA` (0.40), baja por debajo. Al revisar se registra
  un motivo opcional (`Anomalia.MotivoRevision`).
- **Asistente conversacional (RAG)** (`inventario/asistente/`) —
  `indexador.py` construye `DocumentoIndexado` (fichas de producto,
  recomendaciones vigentes, anomalías sin revisar, indicadores del
  periodo, estado del modelo) con embeddings de `sentence-transformers`
  (`paraphrase-multilingual-MiniLM-L12-v2`, local); `recuperador.py` trae
  contexto por similitud coseno; `asistente.py` arma el prompt y llama al
  LLM configurado (`proveedores.py`, Ollama por defecto — ver
  `LLM_PROVEEDOR`/`LLM_MODELO`/`LLM_URL`); `anonimizador.py` redacta
  nombres de clientes, razón social, correos y teléfonos antes de enviar
  cualquier contexto al LLM. El LLM nunca calcula cifras, solo redacta las
  que ya vienen en el contexto recuperado; si el proveedor falla, se
  muestran los datos crudos sin redactar. Comando `indexar_conocimiento`.

Frontend (más allá de las plantillas Django + Bootstrap ya implementadas)
sigue fuera de alcance salvo que el usuario indique lo contrario.

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
