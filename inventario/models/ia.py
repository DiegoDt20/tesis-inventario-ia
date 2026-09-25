"""Modelos del motor de predicción de demanda (entrenamiento y predicciones),
el motor de decisiones (recomendaciones), la detección de anomalías y el
asistente conversacional (RAG)."""
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import models
from pgvector.django import VectorField

from .operacion import ConteoDetalle, Movimiento, Origen, Producto


class NivelPrediccion(models.TextChoices):
    """Granularidad sobre la que se entrenó un modelo o se generó una
    predicción: por producto individual, o por categoría cuando ningún
    producto por sí solo tiene histórico suficiente (ver
    inventario/ml/carga_interna.py y inventario/ml/prediccion.py)."""
    PRODUCTO = 'producto', 'Producto'
    CATEGORIA = 'categoria', 'Categoría'


class ModeloEntrenado(models.Model):
    class Fase(models.TextChoices):
        BASE = 'base', 'Base (preentrenado)'
        AJUSTADO = 'ajustado', 'Ajustado (transferencia)'

    fecha_entrenamiento = models.DateTimeField(auto_now_add=True, db_index=True)
    fase = models.CharField(max_length=10, choices=Fase.choices, db_index=True)
    nivel = models.CharField(
        max_length=10, choices=NivelPrediccion.choices, default=NivelPrediccion.PRODUCTO, db_index=True,
    )
    algoritmo = models.CharField(max_length=100, default='XGBRegressor')
    hiperparametros = models.JSONField()
    mae = models.FloatField()
    rmse = models.FloatField()
    smape = models.FloatField()
    r2 = models.FloatField()
    # Solo aplican al modelo ajustado: sirven para comparar contra la
    # transferencia y demostrar si esta aporta valor.
    mae_linea_base = models.FloatField(null=True, blank=True)
    mae_solo_interno = models.FloatField(null=True, blank=True)
    # Resto de métricas de la comparación de los tres modelos. Null en los
    # modelos "base" y en ajustados entrenados antes de que existieran.
    rmse_linea_base = models.FloatField(null=True, blank=True)
    rmse_solo_interno = models.FloatField(null=True, blank=True)
    smape_linea_base = models.FloatField(null=True, blank=True)
    smape_solo_interno = models.FloatField(null=True, blank=True)
    r2_linea_base = models.FloatField(null=True, blank=True)
    r2_solo_interno = models.FloatField(null=True, blank=True)
    # Un modelo descartado queda en el historial como evidencia de la
    # investigación, pero sus métricas no son válidas (p. ej. un SMAPE
    # desbordado) y no deben leerse como resultado.
    descartado = models.BooleanField(default=False)
    motivo_descarte = models.CharField(max_length=300, blank=True)
    n_registros_externos = models.IntegerField(default=0)
    n_registros_internos = models.IntegerField(default=0)
    # Solo aplica a fase="ajustado": origen de los pedidos internos usados
    # para el ajuste (prueba/real). Null en los modelos "base" (no usan
    # datos internos) y en modelos ajustados entrenados antes de que este
    # campo existiera.
    origen_datos_internos = models.CharField(
        max_length=10, choices=Origen.choices, null=True, blank=True, db_index=True,
    )
    # Solo aplican a fase="ajustado": cuántos días de los datos internos se
    # usaron para entrenar y cuántos para el test temporal (dividir_temporal
    # separa los últimos "dias_prueba" días; el resto es dias_entrenamiento).
    # Null en los modelos "base" y en ajustados entrenados antes de que estos
    # campos existieran. Se muestran en el dashboard porque un conjunto de
    # prueba grande sobre un histórico corto puede dejar muy pocos días para
    # entrenar y volver la comparación de los tres modelos poco confiable
    # (ver MIN_DIAS_ENTRENAMIENTO en el comando entrenar_ajustado).
    dias_entrenamiento = models.IntegerField(null=True, blank=True)
    dias_prueba = models.IntegerField(null=True, blank=True)
    ruta_archivo = models.CharField(max_length=500)
    activo = models.BooleanField(default=True)

    class Meta:
        verbose_name = 'Modelo entrenado'
        verbose_name_plural = 'Modelos entrenados'
        ordering = ['-fecha_entrenamiento']

    def __str__(self):
        return f'{self.get_fase_display()} - {self.fecha_entrenamiento:%Y-%m-%d %H:%M}'


class Prediccion(models.Model):
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE, related_name='predicciones')
    # Sin auto_now_add a propósito: todas las predicciones de una misma
    # corrida de "predecir_demanda" comparten el mismo valor (se pasa
    # explícito), para poder agruparlas como un solo lote más adelante
    # (p. ej. el motor de decisiones toma "el lote más reciente").
    fecha_generacion = models.DateTimeField(db_index=True)
    fecha_objetivo = models.DateField(db_index=True)
    demanda_predicha = models.FloatField()
    demanda_real = models.FloatField(null=True, blank=True)
    modelo = models.ForeignKey(ModeloEntrenado, on_delete=models.PROTECT, related_name='predicciones')
    # Si el modelo predijo a nivel de categoría, esta Prediccion ya viene
    # desagregada a producto (ver inventario/ml/prediccion.py); se guarda
    # igual a nivel de producto para que el motor de decisiones no cambie.
    nivel_prediccion = models.CharField(
        max_length=10, choices=NivelPrediccion.choices, default=NivelPrediccion.PRODUCTO,
    )
    # Participación histórica del producto dentro de su categoría usada para
    # repartir la demanda predicha de la categoría. None cuando
    # nivel_prediccion es "producto" (no hubo reparto).
    participacion_usada = models.FloatField(null=True, blank=True)

    class Meta:
        verbose_name = 'Predicción'
        verbose_name_plural = 'Predicciones'
        ordering = ['-fecha_objetivo']
        indexes = [
            models.Index(fields=['producto', 'fecha_objetivo']),
        ]

    def __str__(self):
        return f'{self.producto.codigo} - {self.fecha_objetivo}'


class Recomendacion(models.Model):
    """Recomendación de reposición generada por el motor de decisiones
    (inventario/decisiones/): lógica determinística, no aprendizaje
    automático. Los números salen de calculos.py; explicacion.py solo los
    redacta en texto."""
    class Estado(models.TextChoices):
        CRITICO = 'critico', 'Crítico'
        REPONER = 'reponer', 'Reponer'
        NORMAL = 'normal', 'Normal'
        EXCESO = 'exceso', 'Exceso'

    producto = models.ForeignKey(Producto, on_delete=models.CASCADE, related_name='recomendaciones')
    # Sin auto_now_add a propósito, igual que Prediccion.fecha_generacion:
    # todas las recomendaciones de una misma corrida de
    # "generar_recomendaciones" comparten el mismo valor (se pasa
    # explícito), para poder agruparlas como un solo lote.
    fecha_generacion = models.DateTimeField(db_index=True)
    estado = models.CharField(max_length=10, choices=Estado.choices, db_index=True)
    stock_actual_snapshot = models.IntegerField()
    demanda_predicha_periodo = models.FloatField()
    desviacion_demanda = models.FloatField()
    lead_time_usado = models.FloatField()
    stock_seguridad = models.FloatField()
    punto_reorden = models.FloatField()
    cantidad_sugerida = models.FloatField()
    nivel_servicio_objetivo = models.FloatField()
    # Datos de entrada que permiten auditar el cálculo desde la pantalla
    # (el "¿Por qué?" de cada tarjeta). Null en recomendaciones generadas
    # antes de que existieran estos campos.
    # Días de predicción sumados en demanda_predicha_periodo (el horizonte
    # de cobertura, o menos si el lote de predicciones no llegaba a tanto).
    dias_horizonte = models.IntegerField(null=True, blank=True)
    # Si la predicción fue a nivel de categoría: demanda predicha de toda la
    # categoría en el mismo horizonte y participación histórica del producto
    # con la que se repartió. Null si se predijo a nivel de producto.
    demanda_categoria_periodo = models.FloatField(null=True, blank=True)
    participacion_usada = models.FloatField(null=True, blank=True)
    # True si lead_time_usado es el promedio real de compras recibidas;
    # False si es el configurado en la ficha del producto.
    lead_time_es_real = models.BooleanField(null=True, blank=True)
    pedidos_en_transito = models.IntegerField(null=True, blank=True)
    explicacion = models.TextField()
    # None = todavía sin decisión; True/False = el usuario la siguió o no.
    # Es el dato clave para la discusión de la tesis (¿se confía en el
    # sistema?), así que se guarda aparte de fecha_generacion.
    aceptada = models.BooleanField(null=True, blank=True)
    fecha_decision = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'Recomendación'
        verbose_name_plural = 'Recomendaciones'
        ordering = ['-fecha_generacion']
        indexes = [
            models.Index(fields=['producto', 'fecha_generacion']),
        ]

    def __str__(self):
        return f'{self.producto.codigo} - {self.get_estado_display()} ({self.fecha_generacion:%Y-%m-%d})'

    @property
    def unidades_sugeridas(self):
        """cantidad_sugerida redondeada a unidades enteras (no se compra
        media lata), con redondeo comercial."""
        return int(Decimal(str(self.cantidad_sugerida)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))

    @property
    def costo_estimado(self):
        """Unidades sugeridas por el costo de compra ACTUAL del producto (no
        el de la fecha de generación: al aprobar la compra importa lo que
        cuesta hoy). None si el producto no tiene costo registrado."""
        if self.producto.costo_compra <= 0:
            return None
        return self.unidades_sugeridas * self.producto.costo_compra

    @property
    def demanda_diaria(self):
        if not self.dias_horizonte:
            return None
        return self.demanda_predicha_periodo / self.dias_horizonte

    @property
    def participacion_pct(self):
        return None if self.participacion_usada is None else self.participacion_usada * 100

    @property
    def dias_cobertura_compra(self):
        """Cuántos días de demanda pronosticada cubre la compra sugerida.
        None si no se conoce la demanda diaria o es cero."""
        demanda_diaria = self.demanda_diaria
        if not demanda_diaria or demanda_diaria <= 0:
            return None
        return self.unidades_sugeridas / demanda_diaria


class Anomalia(models.Model):
    """Anomalía de control de existencias detectada por
    inventario/ml/anomalias.py: diferencias de inventario (Isolation Forest
    + regla del 20%) o movimientos atípicos (z-score sobre el histórico del
    propio producto). Cierra el objetivo específico 1 (control de
    existencias) junto con el indicador EI."""
    class Tipo(models.TextChoices):
        DIFERENCIA_INVENTARIO = 'diferencia_inventario', 'Diferencia de inventario'
        MOVIMIENTO_ATIPICO = 'movimiento_atipico', 'Movimiento atípico'

    class Severidad(models.TextChoices):
        ALTA = 'alta', 'Alta'
        MEDIA = 'media', 'Media'
        BAJA = 'baja', 'Baja'

    class MotivoRevision(models.TextChoices):
        """Por qué se descuadró el inventario, según quien revisó la
        anomalía. Es la causa raíz que el objetivo 1 busca corregir."""
        ERROR_CONTEO = 'error_conteo', 'Error de conteo'
        INGRESO_NO_REGISTRADO = 'ingreso_no_registrado', 'Ingreso no registrado'
        MERMA_NO_ANOTADA = 'merma_no_anotada', 'Merma no anotada'
        OTRO = 'otro', 'Otro'

    producto = models.ForeignKey(Producto, on_delete=models.CASCADE, related_name='anomalias')
    # Solo se completa para tipo=MOVIMIENTO_ATIPICO: el detector de
    # diferencias de inventario nace de un ConteoDetalle, no de un
    # Movimiento puntual. Permite marcar en el listado de movimientos cuál
    # quedó señalado como atípico y enlazar directo a su anomalía.
    movimiento = models.ForeignKey(
        Movimiento, null=True, blank=True, on_delete=models.SET_NULL, related_name='anomalias_detectadas',
    )
    # Solo se completa para tipo=DIFERENCIA_INVENTARIO: la línea del conteo
    # físico que originó la anomalía. Permite agruparlas por conteo en la
    # pantalla y que "detectar_anomalias" actualice la misma anomalía en vez
    # de duplicarla cuando se vuelve a correr.
    conteo_detalle = models.ForeignKey(
        ConteoDetalle, null=True, blank=True, on_delete=models.CASCADE, related_name='anomalias',
    )
    fecha_deteccion = models.DateTimeField(db_index=True)
    tipo = models.CharField(max_length=25, choices=Tipo.choices, db_index=True)
    severidad = models.CharField(max_length=10, choices=Severidad.choices, db_index=True)
    # Isolation Forest: score de anomalía (más alto = más anómalo).
    # Movimiento atípico: |z-score| sobre el histórico del producto.
    score = models.FloatField()
    valor_observado = models.FloatField()
    valor_esperado = models.FloatField()
    descripcion = models.TextField()
    revisada = models.BooleanField(default=False)
    fecha_revision = models.DateTimeField(null=True, blank=True)
    # Opcionales: se piden al marcar como revisada.
    motivo_revision = models.CharField(max_length=25, choices=MotivoRevision.choices, blank=True, db_index=True)
    detalle_revision = models.CharField(max_length=200, blank=True)

    class Meta:
        verbose_name = 'Anomalía'
        verbose_name_plural = 'Anomalías'
        ordering = ['-fecha_deteccion']
        indexes = [
            models.Index(fields=['producto', 'fecha_deteccion']),
        ]

    def __str__(self):
        return f'{self.producto.codigo} - {self.get_tipo_display()} ({self.get_severidad_display()})'


class TipoDocumento(models.TextChoices):
    """Categoría de un DocumentoIndexado, según de qué parte del sistema
    sale el contenido que se indexa para el RAG del asistente
    conversacional."""
    PRODUCTO = 'producto', 'Producto'
    RECOMENDACION = 'recomendacion', 'Recomendación'
    ANOMALIA = 'anomalia', 'Anomalía'
    INDICADOR = 'indicador', 'Indicador'
    MODELO = 'modelo', 'Estado del modelo'
    # Documentos agregados (no vienen de un único registro): existen para
    # que una pregunta general sobre el estado del inventario se responda
    # con una vista de conjunto, en vez de con fichas de producto sueltas
    # traídas por similitud semántica pura (ver
    # inventario/asistente/recuperador.py).
    RESUMEN_CATEGORIA = 'resumen_categoria', 'Resumen por categoría'
    RESUMEN_ESTADO = 'resumen_estado', 'Resumen de estados del catálogo'
    RESUMEN_ANOMALIAS = 'resumen_anomalias', 'Resumen de anomalías'


class DocumentoIndexado(models.Model):
    """Unidad de contexto del índice RAG del asistente conversacional
    (inventario/asistente/): ficha de un producto, una recomendación
    vigente, una anomalía sin revisar, los indicadores de un periodo, o el
    estado del modelo de predicción activo. `embedding` se genera
    localmente con sentence-transformers (inventario/asistente/embeddings.py)
    y se compara por distancia coseno al recuperar contexto para una
    pregunta (inventario/asistente/recuperador.py).

    El comando "indexar_conocimiento" reconstruye esta tabla por completo
    en cada corrida: no se actualiza de forma incremental.
    """
    tipo = models.CharField(max_length=20, choices=TipoDocumento.choices, db_index=True)
    # PK del registro de origen (Producto, Recomendacion, Anomalia...), o
    # None para documentos calculados que no vienen de un único registro
    # (indicadores del periodo, estado del modelo).
    referencia_id = models.IntegerField(null=True, blank=True)
    contenido = models.TextField()
    embedding = VectorField(dimensions=384)
    fecha_indexacion = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Documento indexado'
        verbose_name_plural = 'Documentos indexados'
        indexes = [
            models.Index(fields=['tipo', 'referencia_id']),
        ]

    def __str__(self):
        return f'{self.get_tipo_display()} #{self.referencia_id or "-"}'


class ConsultaAsistente(models.Model):
    """Registro de una pregunta y respuesta del asistente conversacional
    (inventario/asistente/): sirve para auditar qué se usó para responder
    (documentos_usados) y para la discusión de la tesis sobre el uso y la
    confianza en el sistema."""
    pregunta = models.TextField()
    respuesta = models.TextField()
    # Lista de dicts {'tipo', 'referencia_id', 'contenido'} con los
    # DocumentoIndexado recuperados para esta consulta, para poder
    # auditarla sin depender de que esos documentos sigan existiendo tal
    # cual (el índice se reconstruye completo en cada "indexar_conocimiento").
    documentos_usados = models.JSONField(default=list, blank=True)
    fecha = models.DateTimeField(auto_now_add=True, db_index=True)
    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='consultas_asistente',
    )

    class Meta:
        verbose_name = 'Consulta al asistente'
        verbose_name_plural = 'Consultas al asistente'
        ordering = ['-fecha']

    def __str__(self):
        return f'{self.fecha:%Y-%m-%d %H:%M} - {self.pregunta[:50]}'
