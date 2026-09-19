from datetime import datetime, time

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.utils import timezone


class Origen(models.TextChoices):
    """Distingue datos de prueba (pretest/postest de la tesis) de datos reales,
    para poder cargar y limpiar cada conjunto de forma independiente."""
    PRUEBA = 'prueba', 'Prueba'
    REAL = 'real', 'Real'


class Categoria(models.TextChoices):
    """Categoría comercial del producto. Con el volumen actual de ventas
    ningún producto individual alcanza suficientes días con venta al mes
    para predecir su demanda uno por uno; agrupando por categoría sí se
    llega a un histórico entrenable (ver inventario/ml/carga_interna.py)."""
    LATEX = 'latex', 'Látex'
    ESMALTE = 'esmalte', 'Esmalte'
    TEMPLE = 'temple', 'Temple'
    SOLVENTE = 'solvente', 'Solvente'
    ACCESORIO = 'accesorio', 'Accesorio'
    BASE = 'base', 'Base'
    OTRO = 'otro', 'Otro'


class Producto(models.Model):
    codigo = models.CharField(max_length=50, unique=True)
    nombre = models.CharField(max_length=200)
    presentacion = models.CharField(max_length=100, blank=True)
    color = models.CharField(max_length=100, blank=True)
    marca = models.CharField(max_length=100, blank=True)
    categoria = models.CharField(
        max_length=10, choices=Categoria.choices, default=Categoria.OTRO, db_index=True,
    )
    precio_venta = models.DecimalField(max_digits=12, decimal_places=2)
    costo_compra = models.DecimalField(max_digits=12, decimal_places=2)
    stock_actual = models.IntegerField(default=0)
    stock_minimo = models.IntegerField(default=0)
    # Nunca 0: un lead time de cero no tiene sentido físico (ningún
    # proveedor entrega instantáneamente) y anula el cálculo del stock de
    # seguridad (SS = z * sigma * sqrt(lead_time) se vuelve 0).
    lead_time_dias = models.IntegerField(
        default=7,
        validators=[MinValueValidator(1, message='El lead time debe ser mayor a 0 días.')],
    )
    activo = models.BooleanField(default=True)
    origen = models.CharField(max_length=10, choices=Origen.choices, default=Origen.PRUEBA, db_index=True)
    creado_en = models.DateTimeField(auto_now_add=True)
    actualizado_en = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Producto'
        verbose_name_plural = 'Productos'
        ordering = ['nombre']
        constraints = [
            models.CheckConstraint(
                condition=models.Q(lead_time_dias__gt=0),
                name='producto_lead_time_dias_positivo',
            ),
        ]

    def __str__(self):
        return f'{self.codigo} - {self.nombre}'


class Proveedor(models.Model):
    nombre = models.CharField(max_length=200)
    contacto = models.CharField(max_length=200, blank=True)
    telefono = models.CharField(max_length=20, blank=True)
    lead_time_promedio = models.IntegerField(default=0)

    class Meta:
        verbose_name = 'Proveedor'
        verbose_name_plural = 'Proveedores'
        ordering = ['nombre']

    def __str__(self):
        return self.nombre


class Compra(models.Model):
    class Estado(models.TextChoices):
        PENDIENTE = 'pendiente', 'Pendiente'
        RECIBIDA = 'recibida', 'Recibida'
        CANCELADA = 'cancelada', 'Cancelada'

    proveedor = models.ForeignKey(Proveedor, on_delete=models.PROTECT, related_name='compras')
    fecha_pedido = models.DateField(db_index=True)
    fecha_llegada = models.DateField(null=True, blank=True, db_index=True)
    estado = models.CharField(max_length=20, choices=Estado.choices, default=Estado.PENDIENTE)

    class Meta:
        verbose_name = 'Compra'
        verbose_name_plural = 'Compras'
        ordering = ['-fecha_pedido']
        indexes = [
            models.Index(fields=['proveedor', 'fecha_pedido']),
        ]

    def __str__(self):
        return f'Compra #{self.pk} - {self.proveedor.nombre}'

    @property
    def lead_time_real(self):
        if self.fecha_llegada and self.fecha_pedido:
            return (self.fecha_llegada - self.fecha_pedido).days
        return None


def _usuario_sistema_compras():
    """Usuario técnico dueño de los movimientos de ingreso que se generan
    automáticamente al recibir una línea de compra (CompraDetalle.save()),
    para los casos en que no se guarda desde una vista con un usuario real
    (p. ej. scripts o comandos)."""
    Usuario = get_user_model()
    usuario, creado = Usuario.objects.get_or_create(
        username='sistema_compras',
        defaults={'first_name': 'Recepción de compras', 'is_active': False},
    )
    if creado:
        usuario.set_unusable_password()
        usuario.save(update_fields=['password'])
    return usuario


class CompraDetalle(models.Model):
    """Línea de una Compra: una compra real suele traer varios productos a
    la vez, igual que un Pedido trae varios PedidoDetalle."""
    compra = models.ForeignKey(Compra, on_delete=models.CASCADE, related_name='detalles')
    producto = models.ForeignKey(Producto, on_delete=models.PROTECT, related_name='compra_detalles')
    cantidad_pedida = models.IntegerField()
    cantidad_recibida = models.IntegerField(default=0)
    costo_unitario = models.DecimalField(max_digits=12, decimal_places=2)
    # Solo se llena si esta línea llegó en una fecha distinta a la fecha de
    # llegada general de la compra (Compra.fecha_llegada).
    fecha_llegada_linea = models.DateField(null=True, blank=True)

    class Meta:
        verbose_name = 'Detalle de compra'
        verbose_name_plural = 'Detalles de compra'
        indexes = [
            models.Index(fields=['producto', 'compra']),
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Para saber cuánto se recibió de más en este save() y generar el
        # movimiento de ingreso solo por esa diferencia.
        self._cantidad_recibida_original = self.cantidad_recibida if self.pk else 0

    def __str__(self):
        return f'{self.compra} - {self.producto.codigo}'

    @property
    def fecha_llegada_efectiva(self):
        """Fecha de llegada real de esta línea: la propia si se indicó, o
        si no la de la compra completa."""
        return self.fecha_llegada_linea or self.compra.fecha_llegada

    def save(self, *args, usuario=None, **kwargs):
        """Si cantidad_recibida aumenta respecto al valor guardado, genera
        automáticamente un Movimiento de tipo ingreso por la diferencia,
        para que el stock se actualice con la lógica que ya existe en
        Movimiento (y no por fuera de ella escribiendo stock_actual)."""
        diferencia = self.cantidad_recibida - self._cantidad_recibida_original
        with transaction.atomic():
            super().save(*args, **kwargs)
            if diferencia > 0:
                fecha_arribo = self.fecha_llegada_efectiva
                if fecha_arribo:
                    fecha_movimiento = timezone.make_aware(datetime.combine(fecha_arribo, time.min))
                else:
                    fecha_movimiento = timezone.now()
                Movimiento.objects.create(
                    producto=self.producto,
                    tipo=Movimiento.Tipo.INGRESO,
                    cantidad=diferencia,
                    fecha=fecha_movimiento,
                    documento=f'Compra #{self.compra_id}',
                    motivo='Recepción de línea de compra',
                    usuario=usuario or _usuario_sistema_compras(),
                )
        self._cantidad_recibida_original = self.cantidad_recibida


class Movimiento(models.Model):
    class Tipo(models.TextChoices):
        INGRESO = 'ingreso', 'Ingreso'
        SALIDA = 'salida', 'Salida'
        AJUSTE = 'ajuste', 'Ajuste'

    producto = models.ForeignKey(Producto, on_delete=models.PROTECT, related_name='movimientos')
    tipo = models.CharField(max_length=10, choices=Tipo.choices)
    cantidad = models.IntegerField()
    fecha = models.DateTimeField(db_index=True)
    documento = models.CharField(max_length=100, blank=True)
    motivo = models.CharField(max_length=255, blank=True)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='movimientos')
    # Solo se usa para poder revertir un movimiento de tipo "ajuste": ese tipo
    # fija el stock directamente, así que no hay una cantidad que restar/sumar
    # al deshacerlo, se necesita el valor que tenía el producto justo antes.
    stock_anterior = models.IntegerField(null=True, blank=True, editable=False)
    origen = models.CharField(max_length=10, choices=Origen.choices, default=Origen.PRUEBA, db_index=True)

    class Meta:
        verbose_name = 'Movimiento'
        verbose_name_plural = 'Movimientos'
        ordering = ['-fecha']
        indexes = [
            models.Index(fields=['producto', 'fecha']),
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._estado_original = self._snapshot() if self.pk else None

    def __str__(self):
        return f'{self.tipo} - {self.producto.codigo} ({self.cantidad})'

    def _snapshot(self):
        return {
            'producto_id': self.producto_id,
            'tipo': self.tipo,
            'cantidad': self.cantidad,
            'stock_anterior': self.stock_anterior,
        }

    def _resultado(self, stock):
        if self.tipo == self.Tipo.INGRESO:
            return stock + self.cantidad
        if self.tipo == self.Tipo.SALIDA:
            return stock - self.cantidad
        return self.cantidad  # ajuste: fija el valor directamente

    @staticmethod
    def _revertir(stock, snapshot):
        if snapshot is None:
            return stock
        if snapshot['tipo'] == Movimiento.Tipo.INGRESO:
            return stock - snapshot['cantidad']
        if snapshot['tipo'] == Movimiento.Tipo.SALIDA:
            return stock + snapshot['cantidad']
        return snapshot['stock_anterior']  # ajuste: restaurar el valor previo

    def clean(self):
        super().clean()
        if self.tipo != self.Tipo.SALIDA or not self.producto_id:
            return
        stock = self.producto.stock_actual
        if self._estado_original and self._estado_original['producto_id'] == self.producto_id:
            stock = self._revertir(stock, self._estado_original)
        if self._resultado(stock) < 0:
            raise ValidationError({
                'cantidad': (
                    f'Stock insuficiente de {self.producto.codigo}: '
                    f'solo hay {stock} unidades disponibles.'
                )
            })

    def save(self, *args, **kwargs):
        with transaction.atomic():
            producto = Producto.objects.select_for_update().get(pk=self.producto_id)
            original = self._estado_original

            if original and original['producto_id'] != self.producto_id:
                anterior = Producto.objects.select_for_update().get(pk=original['producto_id'])
                anterior.stock_actual = self._revertir(anterior.stock_actual, original)
                anterior.save(update_fields=['stock_actual'])
                original = None

            stock = self._revertir(producto.stock_actual, original)

            if self.tipo == self.Tipo.AJUSTE:
                self.stock_anterior = stock

            nuevo_stock = self._resultado(stock)

            if self.tipo == self.Tipo.SALIDA and nuevo_stock < 0:
                raise ValidationError({
                    'cantidad': (
                        f'Stock insuficiente de {producto.codigo}: '
                        f'esta salida dejaría el stock en {nuevo_stock}.'
                    )
                })

            producto.stock_actual = nuevo_stock
            super().save(*args, **kwargs)
            producto.save(update_fields=['stock_actual'])

        self._estado_original = self._snapshot()

    def delete(self, *args, **kwargs):
        with transaction.atomic():
            producto = Producto.objects.select_for_update().get(pk=self.producto_id)
            producto.stock_actual = self._revertir(producto.stock_actual, self._snapshot())
            producto.save(update_fields=['stock_actual'])
            return super().delete(*args, **kwargs)


class Pedido(models.Model):
    class Canal(models.TextChoices):
        MOSTRADOR = 'mostrador', 'Mostrador'
        WHATSAPP = 'whatsapp', 'WhatsApp'
        TELEFONO = 'telefono', 'Teléfono'

    class Estado(models.TextChoices):
        PENDIENTE = 'pendiente', 'Pendiente'
        ATENDIDO = 'atendido', 'Atendido'
        CANCELADO = 'cancelado', 'Cancelado'

    fecha_solicitud = models.DateTimeField(db_index=True)
    cliente = models.CharField(max_length=200)
    canal = models.CharField(max_length=20, choices=Canal.choices)
    estado = models.CharField(max_length=20, choices=Estado.choices, default=Estado.PENDIENTE)
    origen = models.CharField(max_length=10, choices=Origen.choices, default=Origen.PRUEBA, db_index=True)

    class Meta:
        verbose_name = 'Pedido'
        verbose_name_plural = 'Pedidos'
        ordering = ['-fecha_solicitud']

    def __str__(self):
        return f'Pedido #{self.pk} - {self.cliente}'


class PedidoDetalle(models.Model):
    class MotivoNoAtencion(models.TextChoices):
        SIN_STOCK = 'sin_stock', 'Sin stock'
        LLEGO_TARDE = 'llego_tarde', 'Llegó tarde'
        OTRO = 'otro', 'Otro'

    pedido = models.ForeignKey(Pedido, on_delete=models.CASCADE, related_name='detalles')
    producto = models.ForeignKey(Producto, on_delete=models.PROTECT, related_name='pedido_detalles')
    cantidad_solicitada = models.IntegerField()
    cantidad_atendida = models.IntegerField(default=0)
    # Fecha límite que el cliente pidió (columna "Fecha requerida" de la
    # ficha 2_NS real). Se guarda para poder calcular atendido_a_tiempo más
    # adelante, cuando se completa fecha_atencion desde el listado de
    # pedidos (la solicitud y la atención casi nunca ocurren el mismo día).
    fecha_requerida = models.DateField(null=True, blank=True)
    fecha_atencion = models.DateTimeField(null=True, blank=True, db_index=True)
    atendido_a_tiempo = models.BooleanField(default=False)
    motivo_no_atencion = models.CharField(
        max_length=20, choices=MotivoNoAtencion.choices, null=True, blank=True
    )

    class Meta:
        verbose_name = 'Detalle de pedido'
        verbose_name_plural = 'Detalles de pedido'
        indexes = [
            models.Index(fields=['pedido', 'producto']),
        ]

    def __str__(self):
        return f'{self.pedido} - {self.producto.codigo}'


class ConteoFisico(models.Model):
    fecha_corte = models.DateField(db_index=True)
    responsable = models.CharField(max_length=200)
    observaciones = models.TextField(blank=True)
    origen = models.CharField(max_length=10, choices=Origen.choices, default=Origen.PRUEBA, db_index=True)

    class Meta:
        verbose_name = 'Conteo físico'
        verbose_name_plural = 'Conteos físicos'
        ordering = ['-fecha_corte']

    def __str__(self):
        return f'Conteo {self.fecha_corte} - {self.responsable}'


class ConteoDetalle(models.Model):
    conteo = models.ForeignKey(ConteoFisico, on_delete=models.CASCADE, related_name='detalles')
    producto = models.ForeignKey(Producto, on_delete=models.PROTECT, related_name='conteo_detalles')
    stock_sistema = models.IntegerField()
    stock_fisico = models.IntegerField()
    diferencia = models.IntegerField(editable=False)

    class Meta:
        verbose_name = 'Detalle de conteo'
        verbose_name_plural = 'Detalles de conteo'
        indexes = [
            models.Index(fields=['conteo', 'producto']),
        ]

    def __str__(self):
        return f'{self.conteo} - {self.producto.codigo}'

    def save(self, *args, **kwargs):
        self.diferencia = self.stock_fisico - self.stock_sistema
        super().save(*args, **kwargs)


class Merma(models.Model):
    producto = models.ForeignKey(Producto, on_delete=models.PROTECT, related_name='mermas')
    cantidad = models.IntegerField()
    motivo = models.CharField(max_length=255)
    costo_unitario = models.DecimalField(max_digits=12, decimal_places=2)
    fecha = models.DateField(db_index=True)
    origen = models.CharField(max_length=10, choices=Origen.choices, default=Origen.PRUEBA, db_index=True)

    class Meta:
        verbose_name = 'Merma'
        verbose_name_plural = 'Mermas'
        ordering = ['-fecha']
        indexes = [
            models.Index(fields=['producto', 'fecha']),
        ]

    def __str__(self):
        return f'{self.producto.codigo} - {self.cantidad} ({self.fecha})'


class CostoAlmacenamiento(models.Model):
    periodo_mes = models.DateField(db_index=True, help_text='Usar el primer día del mes correspondiente')
    concepto = models.CharField(max_length=200)
    monto = models.DecimalField(max_digits=12, decimal_places=2)
    origen = models.CharField(max_length=10, choices=Origen.choices, default=Origen.PRUEBA, db_index=True)

    class Meta:
        verbose_name = 'Costo de almacenamiento'
        verbose_name_plural = 'Costos de almacenamiento'
        ordering = ['-periodo_mes']

    def __str__(self):
        return f'{self.periodo_mes} - {self.concepto}'


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
    n_registros_externos = models.IntegerField(default=0)
    n_registros_internos = models.IntegerField(default=0)
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

    producto = models.ForeignKey(Producto, on_delete=models.CASCADE, related_name='anomalias')
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

    class Meta:
        verbose_name = 'Anomalía'
        verbose_name_plural = 'Anomalías'
        ordering = ['-fecha_deteccion']
        indexes = [
            models.Index(fields=['producto', 'fecha_deteccion']),
        ]

    def __str__(self):
        return f'{self.producto.codigo} - {self.get_tipo_display()} ({self.get_severidad_display()})'
