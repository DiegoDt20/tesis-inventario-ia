"""Modelos de costos operativos de inventario (mermas y almacenamiento),
usados para el indicador COI."""
from django.db import models

from .operacion import Origen, Producto


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
