# Sistema de inventario con IA

Sistema web con inteligencia artificial para la gestión de inventario en una
microempresa del sector pintura en Lima. Desarrollado como sistema de tesis
de Ingeniería de Sistemas (diseño preexperimental, pretest/postest sobre el
mismo grupo). Este documento es la referencia técnica del proyecto: describe
la arquitectura, las decisiones de diseño, el modelo de datos y cómo
instalarlo y operarlo.

El sistema cubre: registro de inventario y pedidos, un motor de predicción
de demanda con transferencia de aprendizaje (preentrenamiento con un dataset
externo de Kaggle + ajuste con datos propios de la microempresa), un motor
de decisiones de reposición basado en esas predicciones, detección de
anomalías de control de existencias, y un asistente conversacional (RAG +
LLM local vía Ollama) sobre el estado del inventario.

## Estructura del sistema

```
SISTEMA DE INVENTARIO CON IA
│
├── INTERFAZ — Django Templates
│     ├── Bootstrap 5 + Bootstrap Icons → componentes e iconografía
│     ├── Chart.js → gráficos del dashboard
│     ├── Dashboard → EI, NS y COI con filtro por periodo y origen
│     ├── Operación diaria → pedidos, movimientos, conteos físicos
│     ├── Recomendaciones → reposición explicada, aceptar o rechazar
│     ├── Anomalías → diferencias y movimientos atípicos
│     └── Asistente → chat con fuentes auditables
│
├── BACKEND — Django 5.2 LTS
│     ├── Inventario → productos, stock, movimientos
│     ├── Pedidos → cantidad solicitada vs. atendida
│     ├── Conteos → stock de sistema vs. stock físico
│     ├── Costos → almacenamiento y mermas
│     ├── Indicadores → una sola función por indicador
│     ├── Importación → carga y validación de fichas Excel
│     └── Permisos → grupos administrador y operador
│
├── IA
│     ├── XGBoost → pronóstico de demanda con aprendizaje por transferencia
│     ├── Isolation Forest → detección de anomalías de inventario
│     ├── sentence-transformers → embeddings multilingües locales
│     ├── Ollama + qwen2.5:7b → redacción en lenguaje natural (local)
│     └── RAG → recuperación por similitud sobre datos del sistema
│
├── MOTOR DE DECISIONES (determinístico, no IA)
│     ├── Stock de seguridad
│     ├── Punto de reorden
│     └── Cantidad a pedir
│
└── DATOS
      ├── PostgreSQL 18 → fuente persistente única
      ├── pgvector → búsqueda vectorial para el RAG
      └── Dataset externo → Kaggle Store Item Demand (preentrenamiento)
```

## Decisiones de diseño y su justificación

| Decisión | Alternativa descartada | Razón |
|---|---|---|
| Django Templates | React como proyecto separado | El aporte de la tesis está en la IA; un frontend separado añade semanas sin afectar los indicadores |
| PostgreSQL | MongoDB | Transacciones e integridad referencial garantizan la consistencia del stock, del cual depende el EI |
| Pronóstico por categoría | Pronóstico por producto | Ningún producto superó 4 días con venta al mes; el mínimo viable es 15 |
| Aprendizaje por transferencia | Entrenar solo con datos propios | Un mes de histórico no basta; el modelo sin transferencia apenas iguala a la línea base |
| LLM local (Ollama) | API externa | Confidencialidad de los datos, sin costo y sin dependencia de conexión |
| Motor de decisiones con fórmulas | Decisión por IA | Resultados auditables y respaldados por literatura de gestión de inventario |
| Una función por indicador | Cálculos en cada vista | El dashboard, el importador y el asistente nunca pueden dar valores distintos |

## Arquitectura

```mermaid
flowchart TD
  U[Usuario: dueño u operador] --> V[Vistas Django]
  V --> BD[(PostgreSQL + pgvector)]
  X[Fichas Excel] --> IMP[Importador]
  IMP --> BD
  K[Dataset Kaggle] --> ML[XGBoost: preentrenamiento]
  BD --> ML2[XGBoost: ajuste con datos reales]
  ML --> ML2
  ML2 --> P[Predicciones por categoría]
  P --> DES[Motor de decisiones]
  DES --> REC[Recomendaciones]
  BD --> AN[Isolation Forest]
  AN --> ANO[Anomalías]
  BD --> IDX[Indexador]
  IDX --> VEC[(Embeddings)]
  VEC --> RAG[Recuperador]
  RAG --> LLM[Ollama local]
  LLM --> V
```

### Flujo de operación

1. El operador registra pedidos, movimientos y conteos.
2. El modelo pronostica la demanda de cada categoría apta.
3. El motor de decisiones calcula stock de seguridad, punto de reorden y cantidad a pedir.
4. El detector de anomalías revisa diferencias de inventario y movimientos atípicos.
5. El dashboard presenta los indicadores; el asistente responde consultas sobre los datos.

## Reglas del sistema

Estas reglas son parte del diseño. Cambiarlas altera la validez de los indicadores.

**Integridad del stock.** El stock se actualiza dentro de una transacción con bloqueo del registro. Una salida que deje el stock en negativo se rechaza. Editar o eliminar un movimiento revierte su efecto.

**Demanda real.** El pronóstico se entrena con la cantidad **solicitada**, no con la atendida. Entrenar con lo atendido haría que el modelo aprenda una demanda subestimada y perpetúe el desabastecimiento.

**Separación de orígenes.** Los datos de prueba nunca entran en los indicadores reportados ni en el entrenamiento del modelo vigente. El comando de entrenamiento usa `origen=real` por defecto y registra el origen empleado.

**El modelo de lenguaje no calcula.** Toda cifra que el asistente muestra proviene del contexto recuperado. Si el dato no está en el contexto, el asistente lo indica en lugar de estimarlo. Se verificó que la instrucción por sí sola no basta: lo que evita el cálculo es que el contexto contenga siempre el valor precalculado.

**Falla del servicio de IA.** Si el modelo de lenguaje no responde, el asistente muestra los datos recuperados sin redactar. El sistema no queda inutilizable.

**Anonimización.** Antes de enviar contexto al modelo de lenguaje se eliminan nombres de clientes, razón social, correos y teléfonos. Se mantiene aunque el modelo sea local, para que siga vigente si se cambia a un proveedor externo.

**Validación en la importación.** El importador verifica la coherencia entre las hojas de las fichas, detecta fechas inexistentes y reporta discrepancias antes de escribir. Todo se ejecuta en una transacción: si falla, no queda nada a medias.

## Stack

- Python + Django 5.2
- PostgreSQL con la extensión [pgvector](https://github.com/pgvector/pgvector)
  (necesaria para el índice del asistente conversacional)
- `pandas`, `numpy`, `scikit-learn`, `xgboost` (motor de predicción de demanda)
- `sentence-transformers` (embeddings del asistente conversacional)
- Ollama local (LLM del asistente conversacional; los datos de la
  microempresa no salen del equipo)
- Configuración vía `.env` con `python-decouple`

## Instalación

1. Crear y activar un entorno virtual, e instalar dependencias:

   ```bash
   python -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

2. Crear la base de datos en PostgreSQL con la extensión `pgvector`
   disponible en el servidor (`CREATE EXTENSION IF NOT EXISTS vector;` la
   ejecuta la migración `0013`, pero el paquete `pgvector` debe estar
   instalado en el servidor de PostgreSQL).

3. Copiar `.env.example` a `.env` y completar los valores (ver tabla de
   variables más abajo).

4. Aplicar las migraciones y crear los grupos de permisos:

   ```bash
   python manage.py migrate
   python manage.py crear_grupos_permisos
   python manage.py createsuperuser
   ```

5. Levantar el servidor:

   ```bash
   python manage.py runserver
   ```

6. (Opcional, asistente conversacional) Tener Ollama corriendo localmente
   con el modelo indicado en `LLM_MODELO` descargado (`ollama pull
   qwen2.5:7b`), y regenerar el índice con `indexar_conocimiento`.

## Variables de entorno

Definidas en `.env` (plantilla en `.env.example`); nunca se versiona `.env`
ni se ponen credenciales directamente en `core/settings.py`.

| Variable               | Descripción                                                                          | Valor por defecto / ejemplo         |
|------------------------|---------------------------------------------------------------------------------------|--------------------------------------|
| `SECRET_KEY`           | Clave secreta de Django.                                                              | `changeme-generate-a-new-django-secret-key` |
| `DEBUG`                | Modo debug de Django.                                                                 | `True`                               |
| `ALLOWED_HOSTS`        | Hosts permitidos, separados por coma.                                                 | `localhost,127.0.0.1`                |
| `DB_NAME`              | Nombre de la base de datos PostgreSQL.                                                | `inventario_ia`                      |
| `DB_USER`              | Usuario de PostgreSQL.                                                                | `diego`                              |
| `DB_PASSWORD`          | Contraseña de PostgreSQL.                                                             | *(vacío por defecto)*                |
| `DB_HOST`              | Host de PostgreSQL.                                                                   | `localhost`                          |
| `DB_PORT`              | Puerto de PostgreSQL.                                                                 | `5432`                               |
| `LLM_PROVEEDOR`        | Proveedor del modelo de lenguaje del asistente conversacional.                        | `ollama`                             |
| `LLM_MODELO`           | Modelo servido por el proveedor.                                                      | `qwen2.5:7b`                         |
| `LLM_URL`              | URL base de la API del proveedor (sin `/api/chat`).                                   | `http://localhost:11434`             |
| `EMPRESA_RAZON_SOCIAL` | Opcional: si se define, el anonimizador la redacta antes de enviarla al LLM.          | *(vacío)*                            |
| `PRONOSTICO_TOLERANCIA_RELATIVA` | Tolerancia del acierto del pronóstico: un día acierta si \|real − pronóstico\| / max(real, 1) ≤ este valor. Ver [Exactitud del pronóstico](#exactitud-del-pronóstico). | `0.20` |

## Comandos de gestión

Todos se ejecutan con `python manage.py <comando>`.

| Comando                 | Qué hace                                                                                                          | Parámetros                                                                 |
|--------------------------|---------------------------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------|
| `cargar_datos`           | Importa las fichas de registro (Excel, hojas `1_EI`, `2_NS`, `3_COI_CA`, `3_COI_PD`) a la base de datos.            | `--archivo` (obligatorio), `--origen prueba\|real` (obligatorio), `--limpiar`, `--dry-run` |
| `clasificar_productos`   | Clasifica productos por categoría según palabras clave en el nombre.                                                | `--codigo`, `--categoria` (ambos opcionales; sin ellos reclasifica todos)     |
| `configurar_lead_time`   | Asigna lead time (días) a productos.                                                                                 | `--dias` (obligatorio), `--codigo` (opcional; sin él, solo los que están en 0) |
| `crear_grupos_permisos`  | Crea/actualiza los grupos "administrador" (acceso total) y "operador" (pedidos, movimientos y conteos, sin borrar). | —                                                                              |
| `detectar_anomalias`     | Corre los detectores de anomalías de control de existencias y guarda registros `Anomalia`.                          | `--origen` (opcional)                                                         |
| `entrenar_base`          | Fase 1: preentrena el motor de predicción con el dataset externo de Kaggle.                                          | `--archivo` (por defecto `datos/train.csv`), `--dias-test` (30)               |
| `entrenar_ajustado`      | Fase 2: continúa el entrenamiento del modelo base con datos internos (transferencia) y compara contra línea base y modelo solo-interno. Por defecto con escalado de nivel antes del ajuste fino (ver [Diagnóstico del sesgo de accesorio](#diagnóstico-del-sesgo-de-accesorio)); guarda la regla, los niveles y los factores en `ModeloEntrenado.escalado`. Falla si `--dias-test` dejaría menos de 14 días para entrenar, o si falta el dataset externo con el escalado activado. | `--origen prueba\|real` (por defecto `real`), `--dias-test` (30), `--nivel producto\|categoria` (por defecto `producto`), `--escalar-nivel`/`--no-escalar-nivel` (activado), `--archivo-externo` (`datos/train.csv`) |
| `optimizar_modelo`       | Busca hiperparámetros del ajuste con validación cruzada temporal, prueba variantes de la fase base y de variables, y compara todo sobre el mismo test. **No activa ni registra ningún modelo**; guarda el reporte en `artefactos/optimizacion/` (JSON y CSV). Ver [Optimización del modelo de predicción](#optimización-del-modelo-de-predicción). | `--origen` (`real`), `--nivel` (`categoria`), `--dias-test` (7), `--pliegues` (4), `--iteraciones` (100), `--quitar` (3), `--semilla` (42), `--semillas-ruido` (10), `--archivo-externo` |
| `predecir_demanda`       | Genera predicciones de demanda diaria con el modelo ajustado activo (o el base si aún no hay ajustado).              | `--dias` (30), `--origen` (opcional)                                          |
| `evaluar_predicciones`   | Completa `demanda_real` en predicciones ya vencidas (solo con pedidos del origen y hasta el último día con pedidos de ese origen) y calcula MAE, RMSE, SMAPE y R². Además evalúa la exactitud del modelo ajustado vigente contra tres líneas base, en horizonte diario y de ventana de lead time, con validación de origen móvil (o la división única de antes con `--no-walk-forward`). **No activa ni registra ningún modelo**; guarda el reporte en `artefactos/evaluacion/` (y la división única diaria, también en el vigente). Ver [Exactitud del pronóstico](#exactitud-del-pronóstico). | `--origen` (`real`), `--horizonte diario\|ventana\|ambos` (`ambos`), `--walk-forward`/`--no-walk-forward` (activado) |
| `diagnosticar_sesgo`     | Diagnostica el sesgo sistemático del pronóstico en una categoría: lo caracteriza (signo, orígenes, posición en la ventana), contrasta cuatro hipótesis con una medición y un criterio fijo cada una y, solo si la causa dominante es el modelo base, evalúa el escalado de nivel antes del ajuste fino con el mismo walk-forward. **No toca datos ni activa ni registra modelos**; reporte en `artefactos/diagnostico/`. Ver [Diagnóstico del sesgo de accesorio](#diagnóstico-del-sesgo-de-accesorio). | `--categoria` (`accesorio`), `--origen` (`real`), `--archivo-externo` (`datos/train.csv`) |
| `evaluar_ablacion`       | Estudio de ablación de las fuentes de datos: base sin ajuste con datos de la empresa (con y sin escalado), solo datos internos y el procedimiento del vigente, con las tres líneas base, sobre la misma validación de origen móvil; comparación por origen con prueba de signos y lectura de las tres preguntas. **No toca datos ni activa ni registra modelos**; reporte en `artefactos/ablacion/`. Ver [Estudio de ablación](#estudio-de-ablación). | `--origen` (`real`) |
| `generar_recomendaciones`| Genera una `Recomendacion` de reposición por producto activo con predicciones disponibles.                           | `--nivel-servicio` (0.95), `--dias-cobertura` (30)                            |
| `indexar_conocimiento`   | Regenera el índice del asistente conversacional (borra y reconstruye `DocumentoIndexado`).                           | —                                                                              |
| `recalcular_stock`       | Recalcula `stock_actual` de todos los productos a partir del historial de `Movimiento`.                              | —                                                                              |

## Modelos de datos

Todos en el paquete `inventario/models/` (`operacion.py`, `costos.py`,
`ia.py`), registrados en `admin.py`. El campo
`origen` (`prueba`/`real`) presente en varios modelos permite separar y
limpiar los datos de prueba (pretest/postest) de los datos reales del
negocio.

| Modelo                | Campos principales                                                                                                           |
|------------------------|-------------------------------------------------------------------------------------------------------------------------------|
| `Producto`             | `codigo`, `nombre`, `presentacion`, `color`, `marca`, `categoria`, `precio_venta`, `costo_compra`, `stock_actual`, `stock_minimo`, `lead_time_dias`, `activo`, `origen` |
| `Proveedor`            | `nombre`, `contacto`, `telefono`, `lead_time_promedio`                                                                        |
| `Compra`               | `proveedor` (FK), `fecha_pedido`, `fecha_llegada`, `estado`                                                                   |
| `CompraDetalle`        | `compra` (FK), `producto` (FK), `cantidad_pedida`, `cantidad_recibida`, `costo_unitario`, `fecha_llegada_linea`               |
| `Movimiento`           | `producto` (FK), `tipo` (ingreso/salida/ajuste), `cantidad`, `fecha`, `documento`, `motivo`, `usuario` (FK), `stock_anterior`, `origen` |
| `Pedido`               | `fecha_solicitud`, `cliente`, `canal`, `estado`, `origen`                                                                     |
| `PedidoDetalle`        | `pedido` (FK), `producto` (FK), `cantidad_solicitada`, `cantidad_atendida`, `fecha_requerida`, `fecha_atencion`, `atendido_a_tiempo`, `motivo_no_atencion` |
| `ConteoFisico`         | `fecha_corte`, `responsable`, `observaciones`, `origen`                                                                       |
| `ConteoDetalle`        | `conteo` (FK), `producto` (FK), `stock_sistema`, `stock_fisico`, `diferencia` (calculado)                                     |
| `Merma`                | `producto` (FK), `cantidad`, `motivo`, `costo_unitario`, `fecha`, `origen`                                                    |
| `CostoAlmacenamiento`  | `periodo_mes`, `concepto`, `monto`, `origen`                                                                                  |
| `ModeloEntrenado`      | `fecha_entrenamiento`, `fase` (base/ajustado), `nivel` (producto/categoria), `algoritmo`, `hiperparametros`, `mae`, `rmse`, `smape`, `r2`, `mae_linea_base`, `mae_solo_interno`, `n_registros_externos`, `n_registros_internos`, `origen_datos_internos`, `dias_entrenamiento`, `dias_prueba`, `ruta_archivo`, `activo` |
| `Prediccion`           | `producto` (FK), `fecha_generacion`, `fecha_objetivo`, `demanda_predicha`, `demanda_real`, `modelo` (FK a `ModeloEntrenado`), `nivel_prediccion`, `participacion_usada` |
| `Recomendacion`        | `producto` (FK), `fecha_generacion`, `estado` (crítico/reponer/normal/exceso), `stock_actual_snapshot`, `demanda_predicha_periodo`, `desviacion_demanda`, `lead_time_usado`, `stock_seguridad`, `punto_reorden`, `cantidad_sugerida`, `nivel_servicio_objetivo`, `explicacion`, `aceptada`, `fecha_decision` |
| `Anomalia`             | `producto` (FK), `fecha_deteccion`, `tipo`, `severidad`, `score`, `valor_observado`, `valor_esperado`, `descripcion`, `revisada`, `fecha_revision` |
| `DocumentoIndexado`    | `tipo`, `referencia_id`, `contenido`, `embedding` (vector, 384 dim.), `fecha_indexacion`                                      |
| `ConsultaAsistente`    | `pregunta`, `respuesta`, `documentos_usados` (JSON), `fecha`, `usuario` (FK)                                                  |

Todos los montos monetarios (`precio_venta`, `costo_compra`,
`costo_unitario`, `monto`) son `DecimalField(max_digits=12,
decimal_places=2)`, nunca `FloatField`, para evitar errores de redondeo en
los cálculos de costos.

## Indicadores y trazabilidad con los objetivos de la investigación

| Objetivo específico | Indicador | Fórmula | Tablas que lo sostienen | Módulo |
|---|---|---|---|---|
| OE1 · Control de existencias | EI (exactitud del inventario) | (SRC / TR) × 100 | `Producto`, `Movimiento`, `ConteoFisico`, `ConteoDetalle`, `Anomalia` | Stock automático + detección de anomalías |
| OE2 · Análisis predictivo y atención de pedidos | NS (nivel de servicio) | (PAT / PT) × 100 | `Pedido`, `PedidoDetalle`, `ModeloEntrenado`, `Prediccion` | Pronóstico de demanda |
| OE3 · Automatización y costos | COI (costos operativos de inventario) | CA + PD | `CostoAlmacenamiento`, `PedidoDetalle`, `Recomendacion`, `CompraDetalle` | Motor de decisiones + dashboard |

Donde SRC es stock registrado correctamente, TR total de registros
revisados, PAT pedidos atendidos a tiempo, PT pedidos totales, CA costos de
almacenamiento y PD pérdidas por desabastecimiento.

- **EI** — `ConteoFisico` / `ConteoDetalle` (comparación `stock_sistema` vs
  `stock_fisico`; `diferencia = 0` ⇒ registro correcto).
- **NS** — `Pedido` / `PedidoDetalle.atendido_a_tiempo`.
- **COI** — suma de `CostoAlmacenamiento.monto` en el periodo (costos de
  almacenamiento) más, por cada `PedidoDetalle` no atendido por completo,
  `(cantidad no atendida) × (precio_venta − costo_compra)` del producto
  (pérdidas por desabastecimiento).

  **Las mermas ya están dentro de esa suma**: `cargar_datos` (hoja
  `3_COI_CA`) carga el monto agregado de "Mermas del periodo" como una fila
  más de `CostoAlmacenamiento` (concepto `"Mermas del periodo — ..."`), no
  como registros `Merma` uno por uno — el Excel de esa hoja no trae el
  desglose por producto/cantidad que el modelo `Merma` exige. El modelo
  `Merma` existe para si en el futuro se quiere registrar cada merma
  individualmente (producto, cantidad, motivo, fecha), pero **si
  `calcular_coi()` llegara a sumar también `Merma.cantidad × costo_unitario`
  al COI, el monto de mermas quedaría contado dos veces**: una vez como fila
  agregada de `CostoAlmacenamiento` y otra vez desglosada por `Merma`. Antes
  de instrumentar `Merma` en el cálculo, hay que dejar de cargar "Mermas del
  periodo" como fila de `CostoAlmacenamiento` (o restarla al construir el
  monto de almacenamiento), no simplemente sumar las dos fuentes.

## Optimización del modelo de predicción

Comando `optimizar_modelo` (`inventario/ml/optimizacion.py`). Se corrió el
01/10/2026 sobre los datos reales de agosto 2026, nivel categoría
(accesorio, esmalte, látex), con la misma división que el modelo vigente
(#5): **24 días de ajuste (01/08–24/08) y 7 de prueba (25/08–31/08)**, en
orden cronológico. Se puede repetir tal cual cuando haya más histórico
(`python manage.py optimizar_modelo`, ~2 minutos).

### Qué se probó

1. **Hiperparámetros de la fase de ajuste** — búsqueda aleatoria de 100
   candidatos (`ParameterSampler`, equivalente a `RandomizedSearchCV`) más la
   configuración actual, sobre `n_estimators`, `learning_rate`,
   `max_depth`, `min_child_weight`, `subsample`, `colsample_bytree` y
   `reg_lambda` (espacio en `ESPACIO_AJUSTE`). Se eligió por validación
   cruzada temporal (`TimeSeriesSplit`, 4 pliegues) **solo sobre los 24 días
   de ajuste**: pliegues que entrenan con 8, 12, 16 y 20 días y validan con
   los 4 días siguientes. Los pliegues se arman por fecha, no por fila, para
   que las tres categorías de un mismo día caigan siempre juntas.
2. **Fase base** — 8 combinaciones adicionales de árboles (150, 300, 600) y
   learning rate (0.02, 0.05, 0.1) en el preentrenamiento con Kaggle, cada
   una ajustada con los hiperparámetros de ajuste actuales.
3. **Variables**, cada alternativa por separado: (a) + rezago de 1 día y
   media móvil de 3 días; (b) + indicador de inicio y de fin de mes (5
   primeros / 5 últimos días); (c) − las 3 variables de menor importancia
   por ganancia en el modelo ajustado actual (`dia_mes`, `rezago_30`,
   `std_movil_30`). Como las variables deben ser idénticas entre
   preentrenamiento y ajuste, **cada variante reentrena también la fase
   base** con ese mismo conjunto.
4. **Combinada** — la mejor base y el mejor conjunto de variables *según la
   validación cruzada* (nunca según el test), con su propia búsqueda del
   ajuste.

Garantías de validez: el test no participa de ninguna elección (ni de la
búsqueda, ni de la elección de base/variables, ni del cálculo de
importancias); un test automatizado altera por completo la demanda de los
días de prueba y verifica que nada de lo elegido cambie
(`inventario/tests/test_optimizacion.py`). No se usa shuffle en ningún paso.

Columnas de la tabla: **R² CV** = R² fuera de pliegue de la validación
cruzada (días de ajuste); **MAE/RMSE/SMAPE/R²** = test a un paso (las
variables de cada día usan la demanda real de días anteriores, igual que
`entrenar_ajustado`); **R² rec.** = test pronosticado de forma recursiva
desde el 24/08, sin ver ninguna demanda real del periodo de prueba (así usa
el modelo `predecir_demanda`).

### Resultados (mismo test de 21 filas, ordenados por R²)

| # | Grupo | Configuración | R² CV | MAE | RMSE | SMAPE | R² | R² rec. |
|---|---|---|---:|---:|---:|---:|---:|---:|
| 1 | 2. Base | Base 300 árboles, lr 0.02 | 0.256 | 11.79 | 13.64 | 61.7% | **0.4258** | 0.4504 |
| 2 | 2. Base | Base 150 árboles, lr 0.05 | 0.256 | 12.04 | 13.72 | 62.1% | 0.4193 | 0.4440 |
| 3 | Referencia | Modelo base sin ajuste | 0.224 | 11.82 | 13.74 | 61.1% | 0.4175 | 0.4157 |
| 4 | 2. Base | Base 150 árboles, lr 0.1 | 0.246 | 12.00 | 13.77 | 62.2% | 0.4151 | 0.4482 |
| 5 | Referencia | **Configuración actual (vigente, #5)** | 0.255 | 11.89 | 13.93 | 61.0% | **0.4013** | 0.4542 |
| 6 | 2. Base | Base 150 árboles, lr 0.02 | 0.248 | 12.39 | 14.00 | 62.6% | 0.3950 | 0.4225 |
| 7 | 3. Variables | + inicio y fin de mes | 0.219 | 12.18 | 14.10 | 62.0% | 0.3865 | 0.4161 |
| 8 | 2. Base | Base 600 árboles, lr 0.05 | 0.255 | 11.97 | 14.11 | 60.6% | 0.3860 | 0.4321 |
| 9 | 3. Variables | − dia_mes, rezago_30, std_movil_30 | 0.247 | 11.94 | 14.15 | 61.8% | 0.3819 | 0.3630 |
| 10 | 2. Base | Base 600 árboles, lr 0.02 | 0.233 | 12.19 | 14.19 | 61.8% | 0.3785 | 0.4191 |
| 11 | 3. Variables | + rezago_1 y media_movil_3 | 0.194 | 12.19 | 14.26 | 62.4% | 0.3724 | 0.4017 |
| 12 | 2. Base | Base 300 árboles, lr 0.1 | 0.264 | 12.44 | 14.48 | 62.9% | 0.3530 | 0.3776 |
| 13 | Referencia | Solo datos internos (sin transferencia) | 0.065 | 11.76 | 14.69 | 60.2% | 0.3340 | 0.4775 |
| 14 | 2. Base | Base 600 árboles, lr 0.1 | 0.262 | 12.42 | 14.97 | 61.9% | 0.3083 | 0.3216 |
| 15 | 4. Combinada | Base 300/lr 0.1 + variables actuales + ajuste por CV | 0.336 | 12.13 | 15.92 | 60.9% | 0.2177 | 0.5865 |
| 16 | 1. Ajuste | Ajuste optimizado por CV | 0.323 | 14.51 | 17.70 | 67.5% | 0.0332 | 0.0985 |
| 17 | Referencia | Línea base ingenua (día anterior) | −0.452 | 14.90 | 18.06 | 88.9% | −0.0063 | 0.2096 |

Hiperparámetros elegidos por la validación cruzada:

- Ajuste optimizado (fila 16): `n_estimators=400, learning_rate=0.02,
  max_depth=8, min_child_weight=3, subsample=1.0, colsample_bytree=0.8,
  reg_lambda=10` (actual: `50, 0.01, 6, 1, 0.8, 0.8, 1`).
- Combinada (fila 15): base `300 árboles, lr 0.1`; ajuste
  `n_estimators=25, learning_rate=0.2, max_depth=2, min_child_weight=1,
  subsample=0.5, colsample_bytree=0.8, reg_lambda=0.5`.

**Ruido por semilla.** La configuración actual repetida con 10 semillas
(0–9; solo cambia el azar de `subsample`/`colsample_bytree` en ambas
fases): R² test **0.374 ± 0.019** (rango 0.340–0.398), R² rec. 0.417 ±
0.017, R² CV 0.236 ± 0.011. El 0.4013 reportado por el modelo vigente
(semilla 42) queda por encima de las 10 repeticiones.

### Interpretación

- **El techo de R² con una evaluación válida está en ~0.40.** La mejor fila
  (0.4258, base de 300 árboles con lr 0.02) supera a la vigente en 0.025,
  que es del orden de la variación por semilla (desv. 0.019, rango 0.058).
  Ninguna mejora es atribuible a la configuración.
- **Optimizar por validación cruzada empeoró el test.** La búsqueda del
  ajuste subió el R² CV de 0.255 a 0.323, pero en test cayó a 0.033; la
  combinada, a 0.218. Entre las 14 configuraciones candidatas, el orden por
  R² CV y por R² test no se corresponde (Spearman −0.37, p = 0.19): con
  pliegues de validación de 4 días × 3 categorías (12 filas), la búsqueda
  se ajusta al ruido de esos días. Elegir en cambio la fila de mayor R² en
  test sería ajustar al test.
- **Ninguna variante de variables mejora**: ni el rezago de 1 día ni el
  inicio/fin de mes aportan, y quitar las de menor importancia tampoco.
- **La transferencia sigue aportando frente a entrenar solo con datos
  internos** en la validación cruzada (0.255 vs. 0.065) y en el test a un
  paso (0.401 vs. 0.334), aunque no en el recursivo (0.454 vs. 0.478),
  cuya diferencia también cae dentro del ruido. El ajuste con 24 días casi
  no mueve al modelo base (0.401 vs. 0.418 en test; 0.255 vs. 0.224 en CV).
- **Recomendación** (el comando no activa nada; la decisión es del
  autor): mantener la configuración actual. Con un mes de histórico, lo
  que limita el R² son los datos, no los hiperparámetros. Volver a correr
  `optimizar_modelo` cuando haya al menos 90 días; con más días de
  validación, la búsqueda por CV deja de ajustarse al ruido.

## Exactitud del pronóstico

Comando `evaluar_predicciones` (funciones en `inventario/ml/evaluacion.py`).
MAE, RMSE, SMAPE y R² no permiten afirmar nada sobre *exactitud* en el
sentido que se usa en logística, así que se agregan las métricas estándar
de gestión de inventario y tres líneas base contra las cuales compararlas.
Las definiciones, la tolerancia, el horizonte y los orígenes se fijaron
**antes** de correr la evaluación y no se ajustaron después de ver el
resultado. Por defecto la evaluación es de origen móvil y en dos horizontes
(diario y ventana de lead time); `--no-walk-forward` vuelve a la división
única para reproducir el resultado anterior.

### Métricas

| Métrica | Fórmula | Qué mide |
|---|---|---|
| **WAPE** | Σ \|real − pronóstico\| / Σ real | Tamaño del error relativo al volumen vendido. |
| **Exactitud** | 1 − WAPE | Lo mismo, expresado como acierto. Puede ser negativa (el error supera a la demanda, típico en baja rotación) y se reporta así, sin truncar a 0. |
| **Acierto dentro de tolerancia** | % de filas (días o ventanas) con \|real − pronóstico\| / max(real, 1) ≤ t | Con qué frecuencia el pronóstico cae dentro del margen aceptable. El max(real, 1) evita dividir por cero cuando no hubo demanda. |
| **R² intra-categoría** | R² calculado dentro de cada categoría por separado | Cuánto del movimiento de cada categoría (de día a día, o de ventana a ventana) explica el pronóstico. |
| **Dispersión entre orígenes** | Desviación estándar muestral del WAPE total de cada origen (uno por origen, no por fila) | Cuánto depende el resultado de qué semana se evalúa: 30% ± 4 no es lo mismo que 30% ± 18. |
| **R² global** | R² sobre todas las filas juntas (el que ya se reportaba) | Incluye la diferencia de nivel entre categorías (esmalte ~65 u/día, látex ~10, accesorios ~7): esa varianza entre categorías domina y lo infla. Se mantiene para no romper lo ya reportado. |

- El WAPE **total** es el WAPE agregado sobre todas las filas (Σ \|error\| /
  Σ real), no el promedio de los WAPE por categoría.
- Si una categoría no tuvo demanda en el periodo (Σ real = 0), su WAPE no
  está definido: se reporta como "—" y no entra en el WAPE total. Ninguna
  categoría se excluye por tener baja rotación.
- Sin varianza en la demanda real (o con menos de dos días) el R² tampoco
  está definido y se reporta como "—".

**R² y exactitud miden cosas distintas.** El R² mide cuánto del
movimiento día a día se explica; el WAPE mide el tamaño del error relativo
al volumen. Un pronóstico plano puede tener R² ≈ 0 y aun así un WAPE bajo
si el nivel es correcto, y uno que sigue bien las subidas y bajadas puede
tener un WAPE alto si está desplazado. Para dimensionar pedidos el WAPE es
el más relevante (lo que importa es cuántas unidades sobran o faltan), pero
ninguno reemplaza al otro: se reportan los dos.

### Tolerancia

- **Relativa: t = 0.20** (`PRONOSTICO_TOLERANCIA_RELATIVA` en `.env`). Es la
  misma regla del 20% que el sistema ya usa como diferencia tolerable entre
  stock de sistema y stock físico (detección de anomalías,
  `UMBRAL_DIFERENCIA_RELATIVA`): un solo criterio de negocio para "diferencia
  aceptable" en todo el sistema.
- **Absoluta (en unidades): no se fijó.** No hay compras ni lotes de
  proveedor registrados (`CompraDetalle` vacío), así que no hay un tamaño de
  lote de compra en el cual anclarla. Se evaluó anclarla al stock de
  seguridad que ya calcula el motor de decisiones (SS = z·σ·√L, la cantidad
  que el negocio guarda justamente para absorber el error del pronóstico),
  pero no tiene una traducción directa a un error diario por categoría. El
  SS se calcula por producto y cubre los L = 7 días de lead time, no un día.
  Sumado por categoría (lote vigente de recomendaciones, nivel de servicio
  95%) da ≈150 u (accesorio), ≈804 u (esmalte) y ≈584 u (látex) frente a
  una demanda diaria de ~7, ~65 y ~10 u, así que cualquier pronóstico
  "acertaría". Pasarlo a una tolerancia diaria por categoría (dividir por
  √L, o recalcular σ sobre la serie de la categoría en vez de sumar los σ de
  cada producto) obliga a elegir una conversión, lo que equivale a elegir la
  tolerancia. Se omite hasta que haya lotes de compra reales, o hasta que se
  defina una conversión del SS antes de mirar resultados.

### Horizonte: la ventana de lead time

El pronóstico alimenta el punto de reorden, ROP = d̄·L + SS: lo que el motor
necesita saber es cuánta demanda habrá **durante los L días** que tarda en
llegar el pedido, no cuánto se vende un martes concreto. Por eso se evalúan
dos horizontes por separado:

- **Diario (H = 1):** cada día por separado, como antes. Se mantiene para
  comparar.
- **Ventana de lead time (H = L):** se suma la demanda pronosticada de los L
  días y se compara contra la suma real de esos mismos días. Es el horizonte
  que importa para la decisión. Dentro de una ventana, un día sobrestimado y
  otro subestimado se compensan, igual que en el stock real durante el lead
  time.

L se lee del sistema y no se escribe a mano. Es la **mediana, redondeada
hacia arriba, del lead time que usa el motor de decisiones para el stock de
seguridad** (`lead_time_producto` en `inventario/decisiones/motor.py`): el
promedio real de las compras recibidas si hay al menos 3, y si no, el
`lead_time_dias` de la ficha. Se toman los productos activos de las
categorías evaluadas. Con los datos actuales no hay compras registradas, así
que sale de la ficha: 255 productos con 7 días y 1 con 10, **L = 7**. Se
redondea hacia arriba para que la ventana cubra el lead time completo.

### Validación de origen móvil (walk-forward)

Con 31 días de histórico, una única división (ajuste 01–24/08, prueba
25–31/08: 21 filas) depende de qué semana cayó en la prueba. Diferencias de
1 o 2 puntos de WAPE no son concluyentes. La evaluación sobre un origen de
pronóstico móvil es el método estándar para series cortas (Tashman, L. J.
(2000), "Out-of-sample tests of forecasting accuracy: an analysis and
review", *International Journal of Forecasting*, 16(4), 437–450; Hyndman,
R. J. & Athanasopoulos, G., *Forecasting: Principles and Practice*, 3.ª
ed., sección 5.10 "Time series cross-validation", evaluación sobre un
origen de pronóstico móvil). Para cada origen t
(`inventario/ml/walk_forward.py`):

1. Se rehace el ajuste fino usando **solo los días 1..t**, con la
   configuración del modelo vigente (#5) y sobre el modelo base
   preentrenado con Kaggle. La base no se vuelve a entrenar.
2. Se pronostican los días t+1..t+H **encadenando**: cada día pronosticado
   (con piso en 0) alimenta los rezagos y medias móviles del siguiente,
   como hace `predecir_demanda`. Nunca se usa la demanda real de los días
   intermedios.
3. Se compara contra la demanda real de esos días y se avanza t un día.

El primer origen es el que deja `MIN_DIAS_ENTRENAMIENTO` = 14 días de
ajuste (14/08). El último es el t con t+H = último día con datos. Con 31
días quedan **17 orígenes en el horizonte diario** (14/08–30/08) y **11 en
el de ventana** (14/08–24/08). Se parte por fecha: las tres categorías de
un día van siempre juntas. Con menos de 5 orígenes el comando advierte que
la métrica sigue siendo frágil. Sin ningún origen posible, falla con un
mensaje en vez de dar un número.

Hay dos garantías verificadas por tests (`inventario/tests/test_evaluacion.py`):

- ningún día pronosticado entra en el ajuste de su origen;
- poner 10.000 unidades en todos los días posteriores al origen, o alterar
  un día intermedio de la ventana, no cambia ningún pronóstico de ese
  origen.

Al plantar una fuga a propósito, esos tests fallan.

Las ventanas de orígenes consecutivos comparten 6 de sus 7 días, así que
las 11 ventanas no son 11 muestras independientes. La dispersión entre
orígenes describe cuánto cambia el resultado según la semana; no es un
intervalo de confianza.

### Líneas base

Las tres se evalúan con el mismo procedimiento (mismos orígenes, mismo
horizonte y mismas métricas que el modelo) y solo con la demanda real hasta
el origen:

1. **Media por categoría** de los días de ajuste de ese origen, repetida en
   todo el horizonte. Es la referencia natural del R² intra-categoría.
2. **Media móvil de 7 días** por categoría, encadenada: el primer día usa
   los 7 días reales anteriores al origen; los siguientes incluyen sus
   propios pronósticos en vez de la demanda real.
3. **Último valor** (persistencia): la demanda del día del origen,
   repetida.

El modelo solo puede presentarse como aportando valor si supera a las tres
en WAPE en el **horizonte de ventana**, que es el que importa para la
decisión.

### Resultados con validación de origen móvil

Desde el 03/10/2026 el vigente es el #6, con escalado de nivel. Sus
resultados están en [Adopción del escalado](#adopción-del-escalado-modelo-6).
Lo que sigue es la corrida del modelo anterior (#5, sin escalado), que es
el "antes".

Corrida del 02/10/2026
(`artefactos/evaluacion/evaluacion_walkforward_20261002_060705.json`):
configuración del modelo vigente #5 (ajuste `n_estimators=50,
learning_rate=0.01, max_depth=6, subsample=0.8, colsample_bytree=0.8`)
sobre el modelo base #1, datos reales de agosto 2026, nivel categoría. Las
categorías base, solvente y temple no tienen pronóstico del modelo (menos
de 15 días con venta; se gestionan solo con punto de reorden), así que no
entran en la evaluación. "Filas" = orígenes × categorías. En las filas
**Total**, la columna R² es el R² global; en las demás, el intra-categoría.

**Horizonte diario (H = 1): 17 orígenes, 51 días-categoría.**

| Método | Categoría | Filas | Real | Pronosticado | MAE | RMSE | WAPE | Exactitud | Acierto ±20% | R² | Desv. WAPE entre orígenes |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **Modelo vigente** | accesorio | 17 | 121 | 208.5 | 7.93 | 9.54 | 111.4% | −11.4% | 17.6% | −0.378 | ±24.5 pp |
|  | esmalte | 17 | 666 | 695.7 | 18.94 | 22.33 | 48.3% | 51.7% | 17.6% | −0.124 | ±39.4 pp |
|  | látex | 17 | 581 | 486.0 | 13.53 | 16.29 | 39.6% | 60.4% | 17.6% | −0.174 | ±13.9 pp |
| | **Total** (R² global) | 51 | 1368 | 1390.2 | 13.46 | 16.88 | **50.2%** | **49.8%** | 17.6% | 0.357 | ±16.5 pp |
| Media por categoría (entrenamiento) | accesorio | 17 | 121 | 146.4 | 7.24 | 8.52 | 101.7% | −1.7% | 23.5% | −0.100 | ±30.0 pp |
|  | esmalte | 17 | 666 | 685.7 | 17.64 | 21.67 | 45.0% | 55.0% | 29.4% | −0.058 | ±39.1 pp |
|  | látex | 17 | 581 | 430.0 | 14.04 | 17.69 | 41.1% | 58.9% | 29.4% | −0.384 | ±17.7 pp |
| | **Total** (R² global) | 51 | 1368 | 1262.1 | 12.98 | 16.88 | **48.4%** | **51.6%** | 27.5% | 0.358 | ±22.5 pp |
| Media móvil 7 días | accesorio | 17 | 121 | 93.0 | 6.94 | 9.04 | 97.5% | 2.5% | 11.8% | −0.238 | ±29.1 pp |
|  | esmalte | 17 | 666 | 687.7 | 18.76 | 22.10 | 47.9% | 52.1% | 5.9% | −0.100 | ±34.5 pp |
|  | látex | 17 | 581 | 516.1 | 13.11 | 16.60 | 38.4% | 61.6% | 35.3% | −0.219 | ±20.4 pp |
| | **Total** (R² global) | 51 | 1368 | 1296.9 | 12.94 | 16.79 | **48.2%** | **51.8%** | 17.6% | 0.365 | ±20.9 pp |
| Último valor (día anterior) | accesorio | 17 | 121 | 111.0 | 10.35 | 13.19 | 145.5% | −45.5% | 23.5% | −1.637 | ±63.9 pp |
|  | esmalte | 17 | 666 | 708.0 | 24.94 | 30.68 | 63.7% | 36.3% | 17.6% | −1.122 | ±62.1 pp |
|  | látex | 17 | 581 | 566.0 | 17.35 | 21.57 | 50.8% | 49.2% | 17.6% | −1.059 | ±48.7 pp |
| | **Total** (R² global) | 51 | 1368 | 1385.0 | 17.55 | 22.96 | **65.4%** | **34.6%** | 19.6% | −0.188 | ±35.3 pp |

| Método | WAPE medio por origen | Desv. | Mín. | Máx. | Orígenes |
|---|---:|---:|---:|---:|---:|
| **Modelo vigente** | 51.2% | ±16.5 pp | 20.3% | 80.0% | 17 |
| Media por categoría (entrenamiento) | 49.7% | ±22.5 pp | 25.5% | 112.4% | 17 |
| Media móvil 7 días | 49.8% | ±20.9 pp | 20.5% | 106.2% | 17 |
| Último valor (día anterior) | 69.9% | ±35.3 pp | 22.4% | 158.3% | 17 |

**Horizonte ventana de lead time (H = 7, suma de la ventana): 11 orígenes,
33 ventanas-categoría.**

| Método | Categoría | Filas | Real | Pronosticado | MAE | RMSE | WAPE | Exactitud | Acierto ±20% | R² | Desv. WAPE entre orígenes |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **Modelo vigente** | accesorio | 11 | 589 | 977.8 | 35.34 | 38.13 | 66.0% | 34.0% | 0.0% | −13.122 | ±50.0 pp |
|  | esmalte | 11 | 2933 | 3094.9 | 31.41 | 39.10 | 11.8% | 88.2% | 81.8% | −0.932 | ±8.4 pp |
|  | látex | 11 | 2523 | 2066.6 | 46.49 | 50.17 | 20.3% | 79.7% | 45.5% | −5.229 | ±7.2 pp |
| | **Total** (R² global) | 33 | 6045 | 6139.3 | 37.75 | 42.82 | **20.6%** | **79.4%** | 42.4% | 0.798 | ±4.6 pp |
| Media por categoría (entrenamiento) | accesorio | 11 | 589 | 675.1 | 12.98 | 15.90 | 24.2% | 75.8% | 54.5% | −1.454 | ±27.3 pp |
|  | esmalte | 11 | 2933 | 3121.5 | 32.32 | 38.87 | 12.1% | 87.9% | 81.8% | −0.909 | ±7.5 pp |
|  | látex | 11 | 2523 | 1868.2 | 59.52 | 62.16 | 26.0% | 74.0% | 9.1% | −8.563 | ±6.7 pp |
| | **Total** (R² global) | 33 | 6045 | 5664.8 | 34.94 | 43.31 | **19.1%** | **80.9%** | 48.5% | 0.793 | ±4.0 pp |
| Media móvil 7 días | accesorio | 11 | 589 | 347.1 | 31.00 | 34.84 | 57.9% | 42.1% | 0.0% | −10.788 | ±23.4 pp |
|  | esmalte | 11 | 2933 | 3236.5 | 56.80 | 64.06 | 21.3% | 78.7% | 54.5% | −4.186 | ±11.3 pp |
|  | látex | 11 | 2523 | 2218.3 | 37.48 | 45.05 | 16.3% | 83.7% | 63.6% | −4.023 | ±11.3 pp |
| | **Total** (R² global) | 33 | 6045 | 5801.9 | 41.76 | 49.49 | **22.8%** | **77.2%** | 39.4% | 0.730 | ±7.6 pp |
| Último valor (día anterior) | accesorio | 11 | 589 | 476.0 | 57.73 | 60.56 | 107.8% | −7.8% | 0.0% | −34.616 | ±45.8 pp |
|  | esmalte | 11 | 2933 | 3402.0 | 122.27 | 162.72 | 45.9% | 54.1% | 27.3% | −32.462 | ±41.5 pp |
|  | látex | 11 | 2523 | 2429.0 | 96.73 | 119.15 | 42.2% | 57.8% | 27.3% | −34.136 | ±31.9 pp |
| | **Total** (R² global) | 33 | 6045 | 6307.0 | 92.24 | 121.58 | **50.4%** | **49.6%** | 18.2% | −0.630 | ±27.3 pp |

| Método | WAPE medio por origen | Desv. | Mín. | Máx. | Orígenes |
|---|---:|---:|---:|---:|---:|
| **Modelo vigente** | 20.6% | ±4.6 pp | 13.8% | 26.7% | 11 |
| Media por categoría (entrenamiento) | 19.0% | ±4.0 pp | 14.8% | 26.0% | 11 |
| Media móvil 7 días | 22.9% | ±7.6 pp | 10.3% | 38.4% | 11 |
| Último valor (día anterior) | 50.9% | ±27.3 pp | 18.8% | 98.4% | 11 |

### Interpretación

- **En el horizonte de ventana, el que importa para la decisión, el modelo
  no supera a las tres líneas base.** Supera a la media móvil de 7 días
  (20.6% vs. 22.8% de WAPE) y al último valor (50.4%), pero **no a la media
  por categoría de los días de ajuste** (19.1%). Por la regla fijada de
  antemano, el modelo no puede presentarse como aportando valor frente a
  predecir el promedio de cada categoría. La diferencia con la media (1.5
  puntos) es menor que la dispersión entre orígenes de ambos (±4.6 y ±4.0
  pp), así que tampoco hay evidencia de que sea peor: con este histórico
  son indistinguibles.
- **Por categoría, la desventaja viene de accesorio.** El modelo
  pronostica 978 unidades contra 589 reales en esas ventanas (WAPE 66.0%
  vs. 24.2% de la media). La causa está diagnosticada en
  [Diagnóstico del sesgo de accesorio](#diagnóstico-del-sesgo-de-accesorio). En esmalte empatan (11.8% vs. 12.1%), y en látex
  el modelo es mejor que la media (20.3% vs. 26.0%) aunque no que la media
  móvil (16.3%).
- **El error de la ventana es mucho menor que el diario.** WAPE de 20.6%
  (exactitud 79.4%) frente a 50.2% por día: los errores diarios se
  compensan dentro de la semana. Para dimensionar el punto de reorden, el
  número relevante es el de la ventana.
- **El resultado de la ventana es más estable entre orígenes:** ±4.6 pp
  para el modelo, frente a ±16.5 pp en el horizonte diario.
- **En el horizonte diario el modelo tampoco supera a las medias** (50.2%
  vs. 48.2% la media móvil y 48.4% la media por categoría), lo mismo que
  con la división única. La dispersión diaria (±16.5 a ±22.5 pp) es mucho
  mayor que esas diferencias.
- **El R² intra-categoría de la ventana no es informativo aquí.** Las
  ventanas de orígenes consecutivos comparten 6 de 7 días, así que dentro
  de una categoría sus sumas casi no varían. Cualquier sesgo de nivel
  domina y el R² sale muy negativo para todos los métodos. El R² global de
  0.80 vuelve a reflejar sobre todo la diferencia de nivel entre
  categorías.
- Esto justifica reentrenar y volver a correr `evaluar_predicciones` cuando
  haya más histórico. No se modificó nada para que el modelo supere a las
  líneas base.

### Diagnóstico del sesgo de accesorio

Comando `diagnosticar_sesgo` (`inventario/ml/diagnostico_sesgo.py`). Corrida
del 03/10/2026
(`artefactos/diagnostico/diagnostico_sesgo_accesorio_20261004_032212.json`),
mismos datos, orígenes y configuración que la sección anterior. Es
diagnóstico, no optimización: primero se identifica la causa con una
medición por hipótesis y solo después se evalúa la corrección de esa causa,
una sola variante, definida antes de correrla. Error con signo =
pronóstico − real (positivo = sobreestimación).

**1. Qué tipo de sesgo es.**

| | Accesorio | Esmalte | Látex |
|---|---:|---:|---:|
| Ventana (H = 7): real / pronosticado | 589 / 977.8 | 2933 / 3094.9 | 2523 / 2066.6 |
| Ventana: sesgo relativo Σ(pron. − real) / Σ real | **+66.0%** | +5.5% | −18.1% |
| Ventana: orígenes con sobreestimación | **11 de 11** | 8 de 11 | 1 de 11 |
| Diario (H = 1): real / pronosticado | 121 / 208.5 | 666 / 695.7 | 581 / 486.0 |
| Diario: sesgo relativo | **+72.3%** | +4.5% | −16.3% |

Error medio con signo de accesorio por **posición** dentro de la ventana
(no por fecha): día 1 +5.45, día 2 +6.47, día 3 +5.54, día 4 +5.85, día 5
+3.39, día 6 +3.97, día 7 +4.67 unidades. No crece con la posición: el
sesgo ya está en el primer día pronosticado y **no viene del
encadenamiento**. En horizonte diario, sin encadenar, la proporción es la
misma (+72%).

**2. Hipótesis, en orden.** Criterios fijos (constantes del módulo): una
hipótesis "explica" el sesgo si da cuenta de al menos el 50% de él con el
mismo signo; "muchos ceros" es al menos el doble de días en cero que la
categoría que más tiene entre las demás.

- **(a) El sesgo viene del modelo base: IDENTIFICADA, causa dominante.**
  - El base #1 sin ajustar, en los mismos orígenes, ya pronostica 874.3
    contra 589 en la ventana (+48.4%, 11 de 11 orígenes) y 195.4 contra 121
    en diario (+61.5%). Es el **73% del sesgo** del vigente (+66.0%); el
    ajuste fino agrega el resto. En esmalte (+9.6%) y látex (−19.2%) el
    base no sobreestima de forma sistemática.
  - Mecanismo, medido con la curva de respuesta (pronóstico a un paso con
    el historial constante a cada nivel, fechas de agosto):

    | Nivel del historial (u/día) | 0 | 2 | 5 | 8 | 10 | 15 | 20 | 30 | 40 | 60 |
    |---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
    | Base #1 | 10.8 | 10.8 | 10.8 | 10.9 | 10.9 | 15.6 | 20.2 | 29.6 | 39.1 | 58.6 |
    | Vigente #5 | 11.4 | 11.2 | 11.3 | 10.5 | 10.4 | 16.1 | 20.8 | 30.4 | 33.7 | 52.2 |

    Por debajo de ~10 u/día el modelo **tiene un piso**: pronostica ~11
    aunque el historial sea 0, 2 o 5. Por encima de 15 sigue el nivel.
    Accesorio vive debajo del piso: 8.4 u/día en agosto y 7.1 en los días
    pronosticados. Esmalte (38.4) y látex (28.5) están en la zona donde el
    modelo sigue el nivel. El piso viene del dataset externo: la media
    diaria por serie de Kaggle va de 12.7 a 112.6 (mediana 50.0) y solo el
    0.54% de sus días tiene demanda ≤ 8.4. El base nunca aprendió a
    pronosticar tan bajo, y 50 árboles con learning rate 0.01 no alcanzan
    para borrarlo (la curva del vigente casi no cambia).
- **(b) El piso en cero introduce asimetría: descartada.** Ninguna de las
  231 predicciones del vigente en la ventana fue negativa (0 recortadas en
  las tres categorías); el sesgo con y sin recorte es idéntico. Es
  coherente con (a): el modelo nunca baja de ~10, así que nunca llega a 0.
- **(c) Las medias móviles se degradan con muchos ceros: descartada.**
  Accesorio sí tiene muchos más días en cero (38.7% contra 6.5% de esmalte
  y látex), pero la línea base de media móvil de 7 días, que es
  exactamente esa variable, **subestima** accesorio (−41.1%). Las medias
  móviles con ceros no empujan hacia arriba. Además, por (a), el modelo no
  responde a esas variables por debajo de ~10 u/día.
- **(d) La categoría mezcla productos de rotación distinta: no contrastable
  como causa** (el modelo pronostica la categoría y nunca ve productos);
  se reporta como descripción. De 27 productos activos de accesorio, 13
  tienen pedidos en agosto y 14 ninguno. Los 3 mayores (PIN-083 base de
  rodillo, PIN-041 brocha Tumi, PIN-241 espátula) concentran el 49.8%, y
  ningún producto tiene pedidos en más de 3 días (mediana 1). La demanda
  de accesorio es de pedidos grandes y esporádicos (10 a 30 unidades de un
  producto en un día), no un flujo diario de muchos productos. Es una
  limitación de la agrupación por categoría; no se recategorizó nada.

**3. Corrección de (a), evaluada antes de adoptarla.** Escalado de
nivel antes del ajuste fino (`inventario/ml/escalado.py`): en cada origen,
una serie cuya media diaria en los días de ajuste está por debajo de la
serie externa de menor nivel (12.73 u/día) se multiplica para llevarla a
la mediana externa (50.03 u/día). Se ajusta y pronostica en esa escala, y
el pronóstico se divide por el mismo factor. Así la transferencia aporta la
forma temporal y no el nivel. Las otras series no se tocan: en agosto solo
accesorio queda escalada (factor 5.07 a 6.42 según el origen). Los dos
niveles salen del dataset externo y se fijaron antes de correr la
corrección. Es la única variante evaluada. Se usaron el mismo walk-forward,
los mismos orígenes y las mismas líneas base.

| Ventana (H = 7) | Real | Pronóst. antes → después | Sesgo antes → después | WAPE antes → después | WAPE media cat. | WAPE media móvil 7 | WAPE último valor |
|---|---:|---:|---:|---:|---:|---:|---:|
| accesorio | 589 | 977.8 → 773.2 | +66.0% → +31.3% | 66.0% → 32.8% | 24.2% | 57.9% | 107.8% |
| esmalte | 2933 | 3094.9 → 3144.4 | +5.5% → +7.2% | 11.8% → 13.2% | 12.1% | 21.3% | 45.9% |
| látex | 2523 | 2066.6 → 2177.2 | −18.1% → −13.7% | 20.3% → 16.3% | 26.0% | 16.3% | 42.2% |
| **Total** | 6045 | 6139.3 → 6094.7 | +1.6% → +0.8% | **20.6% → 16.4%** | 19.1% | 22.8% | 50.4% |

| Diario (H = 1) | Real | Pronóst. antes → después | Sesgo antes → después | WAPE antes → después | WAPE media cat. | WAPE media móvil 7 | WAPE último valor |
|---|---:|---:|---:|---:|---:|---:|---:|
| accesorio | 121 | 208.5 → 149.9 | +72.3% → +23.8% | 111.4% → 103.0% | 101.7% | 97.5% | 145.5% |
| esmalte | 666 | 695.7 → 716.4 | +4.5% → +7.6% | 48.3% → 50.7% | 45.0% | 47.9% | 63.7% |
| látex | 581 | 486.0 → 496.3 | −16.3% → −14.6% | 39.6% → 41.0% | 41.1% | 38.4% | 50.8% |
| **Total** | 1368 | 1390.2 → 1362.6 | +1.6% → −0.4% | 50.2% → 51.2% | 48.4% | 48.2% | 65.4% |

- **En la ventana, con el escalado el modelo supera a las tres líneas
  base** (16.4% vs. 19.1%, 22.8% y 50.4%). El WAPE total baja en **11 de
  11 orígenes** (comparación pareada, mismos orígenes) y el sesgo de
  accesorio se reduce a la mitad. Repetida con las semillas 0 a 3 (fuera
  del comando, solo para medir el ruido): antes 19.5–20.7%, después
  15.3–17.0%. La mejora no es ruido de semilla.
- **Lo que no arregla:** accesorio sigue sobreestimado (+31%) y sigue peor
  que la media por categoría (32.8% vs. 24.2%). *Corrección (04/10/2026,
  [Estudio de ablación](#estudio-de-ablación)):* acá se atribuía parte de
  ese sesgo al cambio de nivel de agosto (16 u/día la primera semana, 3.7
  a 6.6 después). La ablación lo desmiente. El base escalado SIN ajuste
  fino, con el mismo historial, deja accesorio en −3.8%: el +31% restante
  lo introduce el ajuste fino.
- **Efecto en las otras categorías:** sus datos de ajuste no cambian (lo
  verifica un test), pero el ajuste fino es conjunto (árboles compartidos)
  y sus pronósticos se mueven. Látex mejora (20.3% → 16.3%, 9 de 11
  orígenes) y esmalte empeora un poco (11.8% → 13.2%, mejora en 3 de 11).
  De la baja neta del error absoluto total (−255 u), −196 vienen de
  accesorio, −101 de látex y +41 de esmalte.
- **En diario no cambia la conclusión.** El total empeora levemente
  (50.2% → 51.2%; baja en 8 de 17 orígenes) y el modelo sigue sin superar
  a las dos medias.
- Se adoptó después de tres verificaciones (sección siguiente). Cambia
  el procedimiento de entrenamiento: hay que reflejarlo en la metodología
  de la tesis.

### Adopción del escalado (modelo #6)

Antes de adoptarlo se verificaron tres cosas (03/10/2026):

1. **Sin fuga en el factor.** El factor de cada origen se calcula con
   `historial_hasta(origen)`, es decir, solo con los días ≤ origen. Lo
   verifican dos tests (`EscaladoSinFugaTests`), iguales a los de fuga del
   walk-forward:
   - el conjunto de días que ve el factor es disjunto del tramo
     pronosticado, y su último día es el origen;
   - llevar a 10.000 unidades los días posteriores al origen no cambia ni
     el factor ni ningún pronóstico. Si esos días entraran, accesorio
     quedaría sobre el mínimo y su factor pasaría a 1.

   En `entrenar_ajustado`, los factores se calculan solo con los días de
   entrenamiento (`entrenar_fase_ajuste_escalado`; lo verifica un test que
   altera los días de prueba).
2. **El objetivo es una regla sobre el dataset externo, no una constante.**
   `escalado.niveles_externos` calcula la mediana de las medias diarias
   por serie de `datos/train.csv`: 50.03 u/día (no 50). Todos los
   resultados se calcularon con ese valor. La media de las medias da
   52.25. No se cambió a ella porque eso sería elegir la regla después de
   ver el resultado.
3. **El criterio es general, no una excepción para accesorio.** Se escala
   cualquier serie cuya media diaria en los días de ajuste quede por
   debajo de la serie externa de menor nivel (12.73 u/día). El piso
   medido del base (salida mínima de la curva de respuesta, grilla de 0.5
   u/día) es **10.76 u/día**: con un historial constante de 0 a 10.5
   u/día, el base pronostica entre 10.76 y 10.93; desde 11 u/día sigue el
   nivel. Las dos reglas clasifican igual en los 17 orígenes:

   | Categoría | Media de ajuste por origen (u/día) | ¿Bajo el piso (10.76)? | ¿Bajo el mínimo externo (12.73)? |
   |---|---|---|---|
   | accesorio | 7.79 – 9.87 | sí | sí |
   | látex | 21.40 – 28.13 | no | no |
   | esmalte | 37.50 – 41.88 | no | no |

   Base (0.83), solvente (3.13) y temple (3.57 u/día en agosto) también
   están debajo del piso, pero no tienen pronóstico del modelo: se
   gestionan por punto de reorden porque tienen menos de 15 días con venta.
   Si alguna llega a 15 días con venta, se escala con la misma regla.

**Aplicación.** `entrenar_ajustado --origen real --nivel categoria
--dias-test 7` (la misma configuración que el #5, ahora con escalado por
defecto) registró y activó el modelo **#6**
(`artefactos/modelos_ml/ajustado_categoria_20261004_035433.json`;
el #5 queda inactivo). Factores con los 24 días de entrenamiento: accesorio
×5.829; esmalte y látex ×1. `predecir_demanda` usa esos factores guardados
en `ModeloEntrenado.escalado`. `evaluar_predicciones` y
`diagnosticar_sesgo` evalúan el vigente con el mismo procedimiento: en el
walk-forward recalculan el factor en cada origen con sus días de ajuste, y
en la división única usan los factores guardados.

Resultado con validación de origen móvil
(`artefactos/evaluacion/evaluacion_walkforward_20261004_035442.json`).
Coincide exactamente con el "después" del diagnóstico.

| Ventana (H = 7), WAPE | Real | Pronóst. #5 → #6 | Modelo #5 | **Modelo #6** | Media por categoría | Media móvil 7 días | Último valor |
|---|---:|---:|---:|---:|---:|---:|---:|
| accesorio | 589 | 977.8 → 773.2 | 66.0% | **32.8%** | 24.2% | 57.9% | 107.8% |
| esmalte | 2933 | 3094.9 → 3144.4 | 11.8% | **13.2%** | 12.1% | 21.3% | 45.9% |
| látex | 2523 | 2066.6 → 2177.2 | 20.3% | **16.3%** | 26.0% | 16.3% | 42.2% |
| **Total** | 6045 | 6139.3 → 6094.7 | 20.6% | **16.4%** | 19.1% | 22.8% | 50.4% |

| Diario (H = 1), WAPE | Real | Pronóst. #5 → #6 | Modelo #5 | **Modelo #6** | Media por categoría | Media móvil 7 días | Último valor |
|---|---:|---:|---:|---:|---:|---:|---:|
| accesorio | 121 | 208.5 → 149.9 | 111.4% | **103.0%** | 101.7% | 97.5% | 145.5% |
| esmalte | 666 | 695.7 → 716.4 | 48.3% | **50.7%** | 45.0% | 47.9% | 63.7% |
| látex | 581 | 486.0 → 496.3 | 39.6% | **41.0%** | 41.1% | 38.4% | 50.8% |
| **Total** | 1368 | 1390.2 → 1362.6 | 50.2% | **51.2%** | 48.4% | 48.2% | 65.4% |

- **En la ventana de lead time, el modelo #6 supera a las tres líneas
  base** (16.4% ±4.7 pp entre orígenes, contra 19.1% ±4.0 de la media por
  categoría). El #5 no las superaba. En la comparación pareada #6 vs. #5,
  el error total baja en los 11 orígenes.
- **Origen por origen contra cada línea base** (`evaluar_predicciones`
  imprime este cálculo y lo guarda como `comparacion_por_origen` en
  `artefactos/evaluacion/evaluacion_walkforward_20261004_041640.json`).
  Se cuenta en cuántos orígenes el WAPE total del #6 es menor y se aplica
  una prueba de signos exacta (binomial, p = 0.5, sin empates):

  | Ventana (H = 7) | #6 supera en | p bilateral | p unilateral | #5 superaba en |
  |---|---:|---:|---:|---:|
  | Media por categoría | **9 de 11** | **0.065** | 0.033 | 4 de 11 (p = 0.549) |
  | Media móvil 7 días | 10 de 11 | 0.012 | 0.006 | — |
  | Último valor | 10 de 11 | 0.012 | 0.006 | — |

  En diario: 6 de 17 contra la media por categoría (p = 0.332), 7 de 17
  contra la media móvil (p = 0.629) y 9 de 17 contra el último valor (p =
  1.000).

  Frase para la tesis: "en la ventana de lead time, el modelo supera a la
  media por categoría en 9 de 11 orígenes (prueba de signos bilateral, p =
  0.065)". Al nivel de 0.05 bilateral **no es significativo**. Además, los
  orígenes consecutivos comparten 6 de sus 7 días pronosticados y casi
  todo el histórico de ajuste: no son independientes y el p real es mayor
  que el informado. Con ventanas sin solapamiento solo habría 2 orígenes en
  agosto. El resultado es favorable pero no concluyente; hace falta más
  histórico.
- **En el horizonte diario sigue sin superar a las dos medias.**
- **La división única de 21 filas (a un paso) queda prácticamente igual:**
  WAPE 45.6% → 46.0%, R² global 0.401 → 0.341, exactitud de accesorio
  −5.7% → 6.0%. Con 21 filas no hay diferencia que leer. Esa corrida es
  la que queda guardada en los campos de evaluación del #6.
- **Accesorio sigue sobreestimado** (+31% en la ventana) y sigue peor que
  la media por categoría. Ese sesgo lo introduce el ajuste fino: el base
  escalado sin ajuste deja accesorio en −3.8% (ver
  [Estudio de ablación](#estudio-de-ablación)).
- `optimizar_modelo` sigue evaluando el procedimiento sin escalado. Sus
  resultados son del #5.

**Recomendaciones con el #6** (03/10/2026). Se corrieron `predecir_demanda
--origen real` (7680 predicciones, 30 días desde el 31/08, modelo #6) y
`generar_recomendaciones` con los mismos parámetros del lote anterior
(27/09, modelo #5: nivel de servicio 0.95, 30 días de cobertura, origen
real). El stock de los 275 productos es el mismo en ambos lotes, así que
las diferencias vienen solo del modelo.

| Estado | Lote anterior (#5) | Lote nuevo (#6) |
|---|---:|---:|
| Crítico | 20 | 20 |
| Reponer | 31 | 31 |
| Normal | 197 | 198 |
| Exceso | 27 | 26 |

- Solo un producto cambia de estado: PIN-142 Vencedor Látex Premium pasa
  de exceso a normal. Su punto de reorden sube de 6.32 a 6.46 con stock 19.
  El cambio es por el reentrenamiento conjunto y no por accesorio.
- **En accesorio no cambia ningún estado, pero baja la cantidad a
  comprar.** Demanda pronosticada a 30 días: 313.3 → 231.3 u (−26%).
  Cantidad sugerida: 244.5 → 172.9 u (−29%). Suma de los puntos de
  reorden: 222.6 → 203.5. El estado no cambia porque el stock de seguridad
  sale de la desviación de la demanda real (igual en los dos lotes) y la
  baja de d̄·L no alcanza a cruzar ningún umbral. Por ejemplo, PIN-083
  sigue crítico con stock 15 y punto de reorden 42.1 (antes 46.8), pero
  con una compra sugerida de 70.2 en vez de 90.2.
- Esmalte y látex suben un poco (cantidad sugerida 624.3 → 649.9 y 679.3 →
  753.8). Látex estaba subestimado (−18% en la ventana) y ahora lo está
  menos (−14%).

### Resultado anterior con la división única

Corrida del 02/10/2026 con `--no-walk-forward`
(`artefactos/evaluacion/evaluacion_categoria_20261002_054829.json`): modelo
vigente #5, 24 días de ajuste (01/08–24/08) y 7 de prueba (25/08–31/08), 21
filas, pronóstico diario a un paso (variables con la demanda real hasta el
día anterior). La opción `--no-walk-forward --horizonte diario` lo
reproduce y es la única corrida que se guarda en los campos de evaluación
de `ModeloEntrenado`.

| | MAE | RMSE | WAPE | Exactitud | Acierto ±20% | R² global | Exact. accesorio | Exact. esmalte | Exact. látex | R² intra accesorio | R² intra esmalte | R² intra látex |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **Modelo vigente** | 11.89 | 13.93 | 45.6% | 54.4% | 19.0% | 0.4013 | −5.7% | 49.9% | 70.9% | −0.2777 | −0.3752 | −0.1374 |
| Media por categoría (entrenamiento) | 11.63 | 14.21 | 44.6% | 55.4% | 33.3% | 0.3769 | 14.0% | 54.1% | 65.2% | −0.0138 | −0.1465 | −0.8247 |
| Media móvil 7 días | 11.37 | 13.42 | **43.6%** | **56.4%** | 23.8% | **0.4447** | 0.3% | 53.2% | 70.9% | −0.2975 | −0.1511 | −0.2041 |
| Último valor (día anterior) | 14.90 | 18.06 | 57.1% | 42.9% | 14.3% | −0.0063 | −71.7% | 57.8% | 52.7% | −2.2268 | −0.6228 | −1.4978 |

Con 21 filas el modelo no superaba a la media móvil ni a la media por
categoría. El R² global de 0.40 se explicaba por la diferencia de nivel
entre categorías: dentro de cada una el R² era negativo.

## Estudio de ablación

¿Funciona el modelo sin los datos de la empresa (solo con Kaggle)? ¿Cuánto
cambia al incorporar agosto? Comando `evaluar_ablacion`
(`inventario/ml/ablacion.py`). Corrida del 04/10/2026
(`artefactos/ablacion/ablacion_20261004_051044.json`), datos reales de
agosto, sobre **la misma validación de origen móvil que
`evaluar_predicciones`**: mismos orígenes, mismas filas y mismos horizontes
(lo verifican los tests). No toca datos ni activa ni registra modelos.

**Precisión: "sin datos de la empresa" no es posible en sentido estricto.**
Aunque el modelo no se entrene con agosto, para pronosticar necesita los
rezagos y medias móviles de los días previos de la propia serie, y esos
días son de agosto. No se puede pronosticar una serie sin su historial. La
configuración A se llama por eso **"sin ajuste con datos de la empresa"**:
el modelo base no se reentrena con agosto, pero recibe el historial de
agosto hasta cada origen como entrada.

**Configuraciones** (incluido el escalado, fijadas antes de correr):

| | Qué es | Escalado |
|---|---|---|
| **A** | Solo externo: el base #1 (Kaggle) tal cual, sin ajuste. | No |
| **A esc.** | Igual que A, con la regla de escalado del vigente (mínimo 12.73, referencia 50.03 u/día; factores con los días ≤ origen). Mide el piso del base sobre datos reales. | Sí |
| **B** | Solo interno: XGBoost desde cero con los días ≤ origen, sin el base, con los hiperparámetros del ajuste fino del #6 (50 árboles, learning rate 0.01, profundidad 6). | No: no hay nivel externo que igualar |
| **C** | Externo + interno: el procedimiento del #6 (ajuste fino del base con los días ≤ origen). | Sí |

Más las tres líneas base. Nota sobre B, dejada por escrito antes de
correr: XGBoost 3.4 parte de la media global del objetivo (todas las
categorías juntas). Con 50 árboles y learning rate 0.01, desde cero solo
puede recorrer una parte del camino hacia el nivel de cada categoría
(cota ≈ 1 − 0.99⁵⁰ ≈ 39%). Es consecuencia de usar la misma configuración
que C, como pide la ablación: no se ajustó para que compita mejor ni peor.

**Ventana de lead time (H = 7, suma): 11 orígenes (14/08–24/08).** WAPE por
categoría con el sesgo relativo entre paréntesis (Σ(pron. − real) / Σ
real). El R² intra-categoría está en la salida del comando y en el
reporte.

| Configuración | Accesorio | Esmalte | Látex | WAPE total | Exactitud | Acierto ±20% | R² global | Desv. WAPE | Sesgo total |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| A. Solo externo | 48.4% (+48.4%) | 16.0% (+9.6%) | 19.7% (−19.2%) | 20.7% | 79.3% | 48.5% | 0.797 | ±4.0 pp | +1.4% |
| A. Solo externo, con escalado | 26.4% (−3.8%) | 16.0% (+9.6%) | 19.7% (−19.2%) | 18.5% | 81.5% | 54.5% | 0.817 | ±3.2 pp | −3.7% |
| B. Solo interno | 171.2% (+171.2%) | 20.0% (−20.0%) | 20.0% (−19.6%) | 34.7% | 65.3% | 36.4% | 0.429 | ±9.4 pp | −1.2% |
| **C. Externo + interno (#6)** | 32.8% (+31.3%) | 13.2% (+7.2%) | 16.3% (−13.7%) | **16.4%** | 83.6% | 63.6% | 0.859 | ±4.7 pp | +0.8% |
| Media por categoría | 24.2% (+14.6%) | 12.1% (+6.4%) | 26.0% (−26.0%) | 19.1% | 80.9% | 48.5% | 0.793 | ±4.0 pp | −6.3% |
| Media móvil 7 días | 57.9% (−41.1%) | 21.3% (+10.3%) | 16.3% (−12.1%) | 22.8% | 77.2% | 39.4% | 0.730 | ±7.6 pp | −4.0% |
| Último valor | 107.8% (−19.2%) | 45.9% (+16.0%) | 42.2% (−3.7%) | 50.4% | 49.6% | 18.2% | −0.630 | ±27.3 pp | +4.3% |

**Diario (H = 1): 17 orígenes (14/08–30/08).**

| Configuración | Accesorio | Esmalte | Látex | WAPE total | Exactitud | Acierto ±20% | R² global | Desv. WAPE | Sesgo total |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| A. Solo externo | 111.1% (+61.5%) | 50.0% (+5.4%) | 38.8% (−17.7%) | 50.6% | 49.4% | 11.8% | 0.343 | ±18.3 pp | +0.5% |
| A. Solo externo, con escalado | 100.7% (+5.1%) | 50.0% (+5.4%) | 38.8% (−17.7%) | 49.7% | 50.3% | 9.8% | 0.353 | ±19.3 pp | −4.5% |
| B. Solo interno | 194.4% (+188.8%) | 46.7% (−23.3%) | 40.6% (−22.1%) | 57.2% | 42.8% | 17.6% | 0.176 | ±20.4 pp | −4.0% |
| **C. Externo + interno (#6)** | 103.0% (+23.8%) | 50.7% (+7.6%) | 41.0% (−14.6%) | 51.2% | 48.8% | 15.7% | 0.338 | ±18.1 pp | −0.4% |
| Media por categoría | 101.7% (+21.0%) | 45.0% (+3.0%) | 41.1% (−26.0%) | 48.4% | 51.6% | 27.5% | 0.358 | ±22.5 pp | −7.7% |
| Media móvil 7 días | 97.5% (−23.1%) | 47.9% (+3.3%) | 38.4% (−11.2%) | 48.2% | 51.8% | 17.6% | 0.365 | ±20.9 pp | −5.2% |
| Último valor | 145.5% (−8.3%) | 63.7% (+6.3%) | 50.8% (−2.6%) | 65.4% | 34.6% | 19.6% | −0.188 | ±35.3 pp | +1.2% |

**Comparación origen por origen** (WAPE total de cada origen; prueba de
signos exacta, p bilateral). Los orígenes consecutivos se solapan:
comparten casi todo el histórico de ajuste y, en la ventana, 6 de sus 7
días pronosticados. **Los p indican consistencia, no son prueba de
significancia.**

| Comparación | Qué mide | Ventana: gana en | p | Diario: gana en | p |
|---|---|---:|---:|---:|---:|
| C vs. A | aporte del ajuste con datos de la empresa (y del escalado) | 10 de 11 | 0.012 | 9 de 17 | 1.000 |
| C vs. A esc. | aporte del ajuste con datos de la empresa (ambos escalados) | 7 de 11 | 0.549 | 6 de 17 | 0.332 |
| C vs. B | aporte de la transferencia desde Kaggle | 10 de 11 | 0.012 | 11 de 17 | 0.332 |
| A vs. B | cuál fuente sola rinde mejor | 10 de 11 | 0.012 | 12 de 17 | 0.143 |
| A esc. vs. A | efecto del escalado sobre el base sin ajuste | 9 de 11 | 0.065 | 11 de 17 | 0.332 |
| A vs. media por categoría | | 4 de 11 | 0.549 | 8 de 17 | 1.000 |
| A esc. vs. media por categoría | | 5 de 11 | 1.000 | 6 de 17 | 0.332 |
| B vs. media por categoría | | 0 de 11 | 0.001 | 6 de 17 | 0.332 |
| C vs. media por categoría | | 9 de 11 | 0.065 | 6 de 17 | 0.332 |

**Lectura.** El criterio es el fijado en la evaluación de la ventana: una
configuración "funciona" si su WAPE total en la ventana es menor que el de
las tres líneas base.

1. **¿Funciona sin ajuste con datos de la empresa (A)? No.** WAPE ventana
   20.7% contra 19.1% de la media por categoría; le gana solo en 4 de 11
   orígenes. **Con escalado, sí en el agregado** (18.5% < 19.1%), pero
   solo en 5 de 11 orígenes (p = 1.000): no es una mejora consistente.
   En diario, ninguna de las dos supera a las medias.
2. **¿Funciona sin la transferencia desde Kaggle (B)? No.** WAPE ventana
   34.7%; pierde contra la media por categoría en los 11 orígenes. El
   problema es accesorio (+171%): partiendo de la media global, B no
   alcanza a bajar al nivel de una categoría de 7–8 u/día. Es el
   resultado con la configuración del ajuste fino, que no está pensada
   para entrenar desde cero.
3. **¿La combinación (C) supera a cada fuente por separado? Sí, a las dos,
   en la ventana:** contra A gana en 10 de 11 orígenes (16.4% vs. 20.7%) y
   contra B también en 10 de 11 (16.4% vs. 34.7%). Es la única
   configuración que supera a las tres líneas base y la única consistente
   contra la media por categoría (9 de 11).

**Lo que esto cambia en lo que la tesis puede afirmar.**

- La ventaja de C sobre A es, sobre todo, **del escalado de nivel y no del
  ajuste con datos de la empresa**. Con el escalado aplicado a los dos
  (C vs. A esc.), el ajuste fino baja el WAPE total de 18.5% a 16.4%, pero
  gana solo en 7 de 11 orígenes (p = 0.549). Por categoría, el ajuste
  fino mejora esmalte (16.0% → 13.2%) y látex (19.7% → 16.3%) y **empeora
  accesorio** (26.4% → 32.8%; sesgo −3.8% → +31.3%). El sesgo que le queda
  al #6 en accesorio lo introduce el ajuste fino; no viene del historial
  (se corrigió la atribución anterior).
- La transferencia sí aporta frente a entrenar solo con agosto (C y A
  superan a B en 10 de 11 orígenes). Con 24–30 días internos y esta
  configuración, sin el base el modelo no llega al nivel de las series
  bajas.
- Lo que se puede escribir: "el modelo base preentrenado, con el nivel de
  cada serie igualado al del dataset externo, ya rinde como la mejor línea
  base. El ajuste con datos de la empresa mejora el agregado en la ventana
  de lead time (16.4% vs. 18.5%), pero no de forma consistente entre
  orígenes."
- El horizonte diario no distingue a ninguna configuración de las medias.

## Limitaciones

- El histórico disponible es de 31 días; la literatura recomienda al menos 90 para series temporales.
- Con solo 31 días, el conjunto de prueba no puede ser grande sin dejar muy
  pocos días para entrenar: el modelo ajustado vigente usa `--dias-test 7`
  (24 días de entrenamiento, 7 de prueba). El propio comando
  `entrenar_ajustado` rechaza una combinación que deje menos de 14 días de
  entrenamiento (`MIN_DIAS_ENTRENAMIENTO`), así que un `--dias-test` mayor
  simplemente no corre con este histórico. Con tan pocos días en cualquiera
  de los dos conjuntos, las métricas van a variar bastante entre
  entrenamientos sucesivos. Medido: con solo cambiar la semilla, el R² en
  test de la configuración vigente varía entre 0.340 y 0.398 (ver
  [Optimización del modelo de predicción](#optimización-del-modelo-de-predicción)).
- El pronóstico por categoría no captura diferencias finas entre productos de una misma categoría. En accesorio, 14 de 27 productos no tuvieron pedidos en agosto y ninguno tuvo pedidos en más de 3 días: la categoría junta pedidos grandes y esporádicos de productos distintos (ver [Diagnóstico del sesgo de accesorio](#diagnóstico-del-sesgo-de-accesorio)).
- El modelo base preentrenado con Kaggle tiene un piso de 10.76 u/día (la serie externa de menor nivel promedia 12.7). El modelo #5 sobreestimaba accesorio por eso (+66% en la ventana de lead time). El vigente #6 escala las series bajo ese rango antes del ajuste fino y lo reduce a +31%, pero accesorio sigue sobreestimado.
- Tres categorías quedan fuera del modelo por volumen insuficiente.
- El detector de movimientos atípicos requiere al menos cinco movimientos previos por producto.
- El modelo de lenguaje local puede producir redacciones imperfectas; las cifras siempre provienen del sistema.
