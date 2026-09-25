"""Modelos de la operación diaria: catálogo, compras, movimientos de stock,
pedidos y conteos físicos."""
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
    # Stock que quedó después de este movimiento, para mostrarlo en el
    # listado sin tener que reconstruir el historial completo del producto.
    # Null en movimientos guardados antes de que este campo existiera.
    stock_resultante = models.IntegerField(null=True, blank=True, editable=False)
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

            self.stock_resultante = nuevo_stock
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
