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
  unitario congelado en la propia línea (`precio_venta_unitario -
  costo_compra_unitario`, fijados al registrar el pedido; NO el precio
  actual del `Producto`), para que el COI del pretest no cambie al
  actualizar precios (pérdidas por desabastecimiento). Las 514 líneas que
  existían al agregar esos campos (migración 0021) se reconstruyeron con
  los precios del producto al 24/09/2026 y tienen
  `precios_reconstruidos=True`: no son el valor histórico real. También se
  marcan así las líneas que quedaron en 0 (pedido importado antes de
  conocer el precio del producto) y se completan con
  `cargar_precios --completar-pedidos`, así que el orden de carga
  (precios antes o después de pedidos) no cambia el COI.
  `calcular_coi()` (`inventario/servicios/indicadores.py`) **no** lee el
  modelo `Merma` directamente. Las mermas ya entran al COI por otra vía: `cargar_datos` (hoja `3_COI_CA`) carga el monto agregado
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
- Comando `cargar_precios --archivo precios.xlsx [--hoja nombre]
  [--completar-pedidos] [--dry-run]`
  (`inventario/management/commands/cargar_precios.py`): actualiza
  `precio_venta`, `costo_compra` y, si el archivo trae la columna,
  `stock_minimo` de productos existentes. No crea productos; reporta
  actualizados, códigos inexistentes y productos activos que siguen sin
  precio (precio de venta o costo de compra en 0). Convierte a número las
  celdas de precio que openpyxl lee como fecha (formato "S/ #,##0.00"),
  con una advertencia por celda. Con `--completar-pedidos` también llena
  las líneas de pedido con precio o costo congelado en 0 y las marca como
  reconstruidas.

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
  - `optimizacion.py` + comando `optimizar_modelo`: búsqueda de
    hiperparámetros del ajuste con `TimeSeriesSplit` por fecha SOLO sobre
    los días de entrenamiento, variantes de la fase base y de variables
    (cada variante de variables reentrena también la base con esas mismas
    columnas), todo medido sobre el mismo test. Nunca activa ni registra
    modelos; el reporte va a `artefactos/optimizacion/`. Resultado
    (01/10/2026, 24/7 días): ninguna configuración supera a la vigente más
    allá del ruido por semilla (R² test 0.374 ± 0.019); no se activó
    nada (la decisión de cambiar el modelo la toma el usuario). Detalle en la sección "Optimización del modelo de predicción"
    del README. Las variables de `COLUMNAS_EXPERIMENTALES`
    (`construir_features(..., experimentales=True)`) no son del modelo
    vigente: `predecir_demanda` sigue usando `COLUMNAS_FEATURES`.
  - Exactitud del pronóstico (`evaluacion.py` + comando
    `evaluar_predicciones --origen`): WAPE (total = agregado, no promedio
    por categoría; None si Σ real = 0), exactitud = 1 − WAPE sin truncar,
    acierto dentro de tolerancia relativa (`PRONOSTICO_TOLERANCIA_RELATIVA`,
    0.20 = regla del 20% de anomalías; fijada antes de evaluar, no se ajusta
    por resultado) y R² intra-categoría junto al global. Tres líneas base
    sobre el mismo test (media por categoría del train, media móvil 7 días,
    último valor). Evalúa el ajustado vigente sobre su propio test temporal
    (`dias_prueba`), guarda la corrida en `ModeloEntrenado` (campos `wape`,
    `exactitud`, ..., read-only en admin) y en `artefactos/evaluacion/`; no
    activa ni registra modelos. Sin tolerancia en unidades (no hay lotes de
    compra; el SS por producto no se traduce a error diario por categoría,
    ver README). Resultado 02/10/2026: el modelo #5 NO supera a la media
    móvil de 7 días ni a la media por categoría en WAPE.
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
  actual del producto. La desviación de la demanda (stock de seguridad) usa solo pedidos
  del origen de `generar_recomendaciones --origen` (`real` por defecto;
  queda en `Recomendacion.origen_demanda`, null en lotes anteriores que
  mezclaban orígenes) y se calcula sobre todos los días del periodo de ese
  origen, con cero los días sin pedidos (no con `features._densificar`,
  que solo rellena entre el primer y el último pedido de cada producto y
  daba desviación 0 a los productos con un solo pedido). Ojo: la demanda
  pronosticada (`Prediccion`) sale de `predecir_demanda --origen`, cuyo
  valor por defecto no filtra por origen: correrlo con `--origen real`
  (`generar_recomendaciones` avisa si el lote parte de otra fecha de corte).
  El motor y el gráfico del dashboard usan solo el último lote de
  predicciones (`Prediccion.fecha_ultimo_lote()`), nunca uno anterior del
  mismo producto. Los productos sin pronóstico en ese lote (categorías que
  no llegan a `MIN_DIAS_VENTA_CATEGORIA` días con venta; con datos reales
  de agosto: solvente, temple y base) no se omiten: se recomiendan con
  `Recomendacion.Metodo.PUNTO_REORDEN` (demanda diaria promedio real en vez
  de la pronosticada, punto de reorden con piso en `stock_minimo`). Cada
  recomendación guarda `metodo` y `fecha_corte_historico` (último día del
  histórico; el pronóstico parte de ahí, no de hoy), y la tarjeta y el
  gráfico los muestran.
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
  LLM configurado (`proveedores.py`: `ollama` local por defecto, o
  `anthropic` = Claude vía API externa con el SDK `anthropic`; ver
  `LLM_PROVEEDOR`/`LLM_MODELO`/`LLM_URL`/`ANTHROPIC_API_KEY`, la clave
  solo en `.env`); `anonimizador.py` redacta nombres de clientes (sin
  distinguir mayúsculas), razón social, correos y teléfonos antes de
  enviar cualquier contexto al LLM, y con Anthropic es obligatoria:
  `ProveedorAnthropic` la vuelve a aplicar a cada mensaje justo antes de
  enviarlo. Lo único que se envía es el prompt de sistema, el contexto
  recuperado y la pregunta, anonimizados. El LLM nunca calcula cifras,
  solo redacta las que ya vienen en el contexto recuperado; si el
  proveedor falla, se muestran los datos crudos sin redactar (y con
  Anthropic la falla queda en el log con el código HTTP).
  `ConsultaAsistente` registra `proveedor_llm`, `modelo_llm` (el que
  informó el servicio) y `fallo_llm`. Comando `indexar_conocimiento`.
  Indexa y responde solo con el origen de `ASISTENTE_ORIGEN` (`real` por
  defecto, como el dashboard): indicadores y rango de fechas de ese
  origen, demanda por producto sin pedidos de otro origen, y anomalías
  filtradas por el origen de su conteo/movimiento (no por el del
  producto; `Anomalia.objects.del_origen()`, el mismo filtro que usa el
  dashboard). No califica cifras ("bajo", "alto") salvo que la
  calificación o el umbral vengan en el contexto. Cada `DocumentoIndexado` guarda su `origen` y el recuperador
  solo trae los del origen configurado; tras cambiarlo, reindexar.

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
