from django.contrib import admin
from django.utils import timezone

from .models import (
    Anomalia,
    Compra,
    CompraDetalle,
    ConteoDetalle,
    ConteoFisico,
    CostoAlmacenamiento,
    Merma,
    ModeloEntrenado,
    Movimiento,
    Pedido,
    PedidoDetalle,
    Prediccion,
    Producto,
    Proveedor,
    Recomendacion,
)


@admin.register(Producto)
class ProductoAdmin(admin.ModelAdmin):
    list_display = (
        'codigo', 'nombre', 'presentacion', 'marca', 'color', 'categoria', 'precio_venta',
        'costo_compra', 'stock_actual', 'stock_minimo', 'activo', 'origen',
    )
    list_filter = ('activo', 'categoria', 'marca', 'color', 'origen')
    search_fields = ('codigo', 'nombre', 'marca')
    readonly_fields = ('stock_actual',)


@admin.register(Proveedor)
class ProveedorAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'contacto', 'telefono', 'lead_time_promedio')
    search_fields = ('nombre', 'contacto')


class CompraDetalleInline(admin.TabularInline):
    model = CompraDetalle
    extra = 1
    fields = ('producto', 'cantidad_pedida', 'cantidad_recibida', 'costo_unitario', 'fecha_llegada_linea')


@admin.register(Compra)
class CompraAdmin(admin.ModelAdmin):
    list_display = ('id', 'proveedor', 'fecha_pedido', 'fecha_llegada', 'estado', 'lead_time_real')
    list_filter = ('estado', 'proveedor')
    search_fields = ('proveedor__nombre',)
    date_hierarchy = 'fecha_pedido'
    inlines = [CompraDetalleInline]

    def save_formset(self, request, form, formset, change):
        # CompraDetalle.save() genera el Movimiento de ingreso al recibir
        # una línea; se le pasa el usuario real de la sesión de admin en
        # vez de dejar que use el usuario técnico por defecto.
        if formset.model is not CompraDetalle:
            formset.save()
            return
        instancias = formset.save(commit=False)
        for eliminado in formset.deleted_objects:
            eliminado.delete()
        for instancia in instancias:
            instancia.save(usuario=request.user)
        formset.save_m2m()


@admin.register(Movimiento)
class MovimientoAdmin(admin.ModelAdmin):
    list_display = ('producto', 'tipo', 'cantidad', 'fecha', 'documento', 'usuario', 'origen')
    list_filter = ('tipo', 'fecha', 'origen')
    search_fields = ('producto__codigo', 'producto__nombre', 'documento', 'motivo')
    date_hierarchy = 'fecha'


@admin.register(Pedido)
class PedidoAdmin(admin.ModelAdmin):
    list_display = ('id', 'cliente', 'canal', 'estado', 'fecha_solicitud', 'origen')
    list_filter = ('canal', 'estado', 'origen')
    search_fields = ('cliente',)
    date_hierarchy = 'fecha_solicitud'


@admin.register(PedidoDetalle)
class PedidoDetalleAdmin(admin.ModelAdmin):
    list_display = (
        'pedido', 'producto', 'cantidad_solicitada', 'cantidad_atendida',
        'atendido_a_tiempo', 'motivo_no_atencion', 'fecha_atencion',
    )
    list_filter = ('atendido_a_tiempo', 'motivo_no_atencion')
    search_fields = ('pedido__cliente', 'producto__codigo', 'producto__nombre')


@admin.register(ConteoFisico)
class ConteoFisicoAdmin(admin.ModelAdmin):
    list_display = ('fecha_corte', 'responsable', 'origen')
    list_filter = ('origen',)
    search_fields = ('responsable',)
    date_hierarchy = 'fecha_corte'


@admin.register(ConteoDetalle)
class ConteoDetalleAdmin(admin.ModelAdmin):
    list_display = ('conteo', 'producto', 'stock_sistema', 'stock_fisico', 'diferencia')
    list_filter = ('conteo',)
    search_fields = ('producto__codigo', 'producto__nombre')


@admin.register(Merma)
class MermaAdmin(admin.ModelAdmin):
    list_display = ('producto', 'cantidad', 'motivo', 'costo_unitario', 'fecha', 'origen')
    list_filter = ('fecha', 'origen')
    search_fields = ('producto__codigo', 'producto__nombre', 'motivo')
    date_hierarchy = 'fecha'


@admin.register(CostoAlmacenamiento)
class CostoAlmacenamientoAdmin(admin.ModelAdmin):
    list_display = ('periodo_mes', 'concepto', 'monto', 'origen')
    list_filter = ('periodo_mes', 'origen')
    search_fields = ('concepto',)


@admin.register(ModeloEntrenado)
class ModeloEntrenadoAdmin(admin.ModelAdmin):
    list_display = (
        'fase', 'nivel', 'fecha_entrenamiento', 'algoritmo', 'mae', 'rmse', 'smape',
        'r2', 'mae_linea_base', 'mae_solo_interno', 'activo',
    )
    list_filter = ('fase', 'nivel', 'activo')
    readonly_fields = ('fecha_entrenamiento',)
    date_hierarchy = 'fecha_entrenamiento'


@admin.register(Prediccion)
class PrediccionAdmin(admin.ModelAdmin):
    list_display = (
        'producto', 'fecha_objetivo', 'demanda_predicha', 'demanda_real',
        'nivel_prediccion', 'participacion_usada', 'modelo', 'fecha_generacion',
    )
    list_filter = ('modelo', 'nivel_prediccion')
    search_fields = ('producto__codigo', 'producto__nombre')
    date_hierarchy = 'fecha_objetivo'


@admin.register(Recomendacion)
class RecomendacionAdmin(admin.ModelAdmin):
    list_display = (
        'producto', 'estado', 'stock_actual_snapshot', 'punto_reorden',
        'cantidad_sugerida', 'aceptada', 'fecha_generacion',
    )
    list_filter = ('estado', 'aceptada')
    search_fields = ('producto__codigo', 'producto__nombre')
    readonly_fields = ('fecha_generacion', 'explicacion')
    date_hierarchy = 'fecha_generacion'
    actions = ['marcar_aceptada', 'marcar_rechazada']

    @admin.action(description='Marcar como aceptada')
    def marcar_aceptada(self, request, queryset):
        actualizadas = queryset.update(aceptada=True, fecha_decision=timezone.now())
        self.message_user(request, f'{actualizadas} recomendación(es) marcada(s) como aceptada(s).')

    @admin.action(description='Marcar como rechazada')
    def marcar_rechazada(self, request, queryset):
        actualizadas = queryset.update(aceptada=False, fecha_decision=timezone.now())
        self.message_user(request, f'{actualizadas} recomendación(es) marcada(s) como rechazada(s).')


@admin.register(Anomalia)
class AnomaliaAdmin(admin.ModelAdmin):
    list_display = (
        'producto', 'tipo', 'severidad', 'score', 'valor_observado',
        'valor_esperado', 'revisada', 'fecha_deteccion',
    )
    list_filter = ('tipo', 'severidad', 'revisada')
    search_fields = ('producto__codigo', 'producto__nombre', 'descripcion')
    readonly_fields = ('fecha_deteccion',)
    date_hierarchy = 'fecha_deteccion'
    actions = ['marcar_revisada']

    @admin.action(description='Marcar como revisada')
    def marcar_revisada(self, request, queryset):
        actualizadas = queryset.update(revisada=True, fecha_revision=timezone.now())
        self.message_user(request, f'{actualizadas} anomalía(s) marcada(s) como revisada(s).')
