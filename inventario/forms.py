"""Formularios de inventario.

Las pantallas de operación diaria (pedidos, movimientos, conteos) están
pensadas para el dueño de la microempresa, sin conocimientos técnicos: los
formularios validan de más antes que de menos, y los mensajes de error se
redactan en lenguaje sencillo.
"""
import hashlib
from datetime import date

from django import forms
from django.conf import settings
from django.contrib.auth.forms import AuthenticationForm
from django.core.cache import cache
from django.core.exceptions import ValidationError

from .models import Compra, Movimiento, Pedido, PedidoDetalle, Producto, Proveedor


class LoginForm(AuthenticationForm):
    """Inicio de sesión con bloqueo temporal: tras LOGIN_MAX_INTENTOS
    intentos fallidos con el mismo nombre de usuario, ese usuario queda
    bloqueado LOGIN_BLOQUEO_MINUTOS minutos (ni siquiera la contraseña
    correcta entra), para frenar la prueba de contraseñas por fuerza bruta
    cuando el sistema está publicado. Se cuenta por usuario y no por IP:
    detrás del túnel todas las peticiones llegan desde la misma dirección.
    Un ingreso correcto reinicia el contador."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bloqueado = False
        for campo in self.fields.values():
            campo.widget.attrs.setdefault('class', 'form-control')

    @staticmethod
    def _clave_intentos(usuario):
        # Hash: el nombre de usuario puede traer caracteres que una caché
        # no acepta como clave.
        return 'login-fallos:' + hashlib.sha256(usuario.strip().lower().encode()).hexdigest()

    def clean(self):
        clave = self._clave_intentos(self.cleaned_data.get('username') or '')
        if cache.get(clave, 0) >= settings.LOGIN_MAX_INTENTOS:
            self.bloqueado = True
            raise ValidationError(
                f'Demasiados intentos fallidos. Espera {settings.LOGIN_BLOQUEO_MINUTOS} minutos '
                'y vuelve a intentarlo.',
                code='bloqueado',
            )
        try:
            datos = super().clean()
        except ValidationError:
            # add no pisa un contador existente: el plazo corre desde el
            # primer fallo.
            cache.add(clave, 0, settings.LOGIN_BLOQUEO_MINUTOS * 60)
            try:
                cache.incr(clave)
            except ValueError:
                # Venció entre add e incr.
                cache.set(clave, 1, settings.LOGIN_BLOQUEO_MINUTOS * 60)
            raise
        cache.delete(clave)
        return datos


def resolver_producto(texto):
    """Busca un producto a partir del texto elegido en el buscador (una
    opción de la forma "CODIGO — nombre..."): toma el código, que es lo que
    va antes del primer guion largo. Devuelve el Producto o None si no se
    encontró (texto vacío, o no coincide con ningún código)."""
    if not texto:
        return None
    codigo = texto.split(' — ')[0].strip()
    return Producto.objects.filter(codigo=codigo).first()


def etiqueta_producto(producto):
    """Texto que se muestra (y se usa como valor) en el buscador de
    producto: "CODIGO — nombre presentación color", igual en todas las
    pantallas que buscan producto."""
    partes = [producto.codigo, '—', producto.nombre]
    if producto.presentacion:
        partes.append(producto.presentacion)
    if producto.color:
        partes.append(producto.color)
    return ' '.join(partes)


ATRIBUTOS_BUSCADOR_PRODUCTO = {
    'class': 'form-control', 'list': 'lista-productos',
    'placeholder': 'Código o nombre del producto', 'autocomplete': 'off',
}

# Los tres campos que MovimientoForm necesita para la validación en vivo de
# stock (producto, tipo, cantidad) se incluyen entre sí con hx-include:
# cambiar cualquiera de los tres vuelve a pedir /movimientos/validar/ con
# los valores actuales de los otros dos. A nivel de módulo (no de clase)
# porque MovimientoForm.Meta, al ser una clase anidada, no ve los nombres
# del cuerpo de MovimientoForm.
ATRIBUTOS_VALIDACION_STOCK = {
    'hx-get': '/movimientos/validar/',
    'hx-target': '#aviso-stock',
    'hx-include': '#id_producto,#id_tipo,#id_cantidad',
}

# Validación en vivo de una línea de pedido (cantidad atendida vs.
# solicitada): "closest .linea-pedido" incluye los tres campos de la MISMA
# línea del formset (ver el wrapper en pedido_form.html), y "next
# .aviso-linea" apunta al aviso de esa misma línea, sin depender del índice
# del formset (form-0-, form-1-...).
ATRIBUTOS_VALIDACION_LINEA_PEDIDO = {
    'hx-get': '/pedidos/validar-linea/',
    'hx-target': 'next .aviso-linea',
    'hx-include': 'closest .linea-pedido',
}


def evaluar_linea_pedido(cantidad_solicitada, cantidad_atendida, motivo_no_atencion):
    """Valida la relación entre cantidad solicitada y atendida de una línea
    de pedido. Devuelve (campo, mensaje) si hay un problema, o None si está
    bien. La usan LineaPedidoForm.clean(), CompletarLineaPedidoForm.clean()
    y la validación en vivo de pedido_validar_linea (vista), para que las
    tres nunca digan algo distinto sobre la misma línea."""
    if cantidad_solicitada is None:
        return None
    if cantidad_atendida > cantidad_solicitada:
        return ('cantidad_atendida', 'No puede ser mayor que la cantidad solicitada.')
    if cantidad_atendida < cantidad_solicitada and not motivo_no_atencion:
        return ('motivo_no_atencion', 'La cantidad atendida es menor que la solicitada: indica el motivo.')
    return None


class PedidoForm(forms.Form):
    """Cabecera del pedido (item.pedidos_detalle va aparte, en
    LineaPedidoFormSet): fecha, cliente y canal."""
    fecha_solicitud = forms.DateField(
        label='Fecha del pedido', initial=date.today,
        widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
    )
    cliente = forms.CharField(
        label='Cliente', max_length=200,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Nombre del cliente'}),
    )
    canal = forms.ChoiceField(
        label='Canal', choices=Pedido.Canal.choices,
        widget=forms.Select(attrs={'class': 'form-select'}),
    )


class LineaPedidoForm(forms.Form):
    """Una línea del pedido: un producto, lo que pidió y lo que se le
    entregó. Si se entregó menos de lo pedido, exige el motivo."""
    producto = forms.CharField(
        label='Producto',
        widget=forms.TextInput(attrs=ATRIBUTOS_BUSCADOR_PRODUCTO),
    )
    cantidad_solicitada = forms.IntegerField(
        label='Cant. solicitada', min_value=1,
        widget=forms.NumberInput(attrs={
            'class': 'form-control', **ATRIBUTOS_VALIDACION_LINEA_PEDIDO, 'hx-trigger': 'change',
        }),
    )
    cantidad_atendida = forms.IntegerField(
        label='Cant. atendida', min_value=0, required=False, initial=0,
        widget=forms.NumberInput(attrs={
            'class': 'form-control',
            **ATRIBUTOS_VALIDACION_LINEA_PEDIDO, 'hx-trigger': 'input changed delay:400ms, change',
        }),
    )
    fecha_requerida = forms.DateField(
        label='Fecha requerida', required=False,
        widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
    )
    motivo_no_atencion = forms.ChoiceField(
        label='Motivo si no se entregó todo',
        choices=[('', '—')] + list(PedidoDetalle.MotivoNoAtencion.choices),
        required=False,
        widget=forms.Select(attrs={
            'class': 'form-select', **ATRIBUTOS_VALIDACION_LINEA_PEDIDO, 'hx-trigger': 'change',
        }),
    )

    def clean_producto(self):
        texto = self.cleaned_data['producto']
        producto = resolver_producto(texto)
        if producto is None:
            raise forms.ValidationError('Selecciona un producto de la lista (busca por código o nombre).')
        return producto

    def clean(self):
        cleaned = super().clean()
        solicitada = cleaned.get('cantidad_solicitada')
        atendida = cleaned.get('cantidad_atendida')
        if atendida is None:
            atendida = 0
            cleaned['cantidad_atendida'] = atendida

        error = evaluar_linea_pedido(solicitada, atendida, cleaned.get('motivo_no_atencion'))
        if error:
            self.add_error(*error)
        return cleaned


LineaPedidoFormSet = forms.formset_factory(LineaPedidoForm, extra=1)


class CompletarLineaPedidoForm(forms.Form):
    """Completa la atención de una línea de pedido pendiente, desde el
    listado de pedidos. atendido_a_tiempo no se pregunta: se calcula solo
    comparando fecha_atencion con fecha_requerida."""
    cantidad_atendida = forms.IntegerField(
        label='Cant. atendida', min_value=0,
        widget=forms.NumberInput(attrs={'class': 'form-control form-control-sm'}),
    )
    fecha_atencion = forms.DateField(
        label='Fecha de atención', initial=date.today,
        widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-control form-control-sm'}),
    )
    motivo_no_atencion = forms.ChoiceField(
        label='Motivo si no se entregó todo',
        choices=[('', '—')] + list(PedidoDetalle.MotivoNoAtencion.choices),
        required=False,
        widget=forms.Select(attrs={'class': 'form-select form-select-sm'}),
    )

    def __init__(self, *args, cantidad_solicitada=None, **kwargs):
        self.cantidad_solicitada = cantidad_solicitada
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned = super().clean()
        atendida = cleaned.get('cantidad_atendida')
        if atendida is not None:
            error = evaluar_linea_pedido(self.cantidad_solicitada, atendida, cleaned.get('motivo_no_atencion'))
            if error:
                self.add_error(*error)
        return cleaned


class MovimientoForm(forms.ModelForm):
    """Registro de un movimiento de stock. producto se resuelve con el
    mismo buscador que los pedidos; el resto de campos son del modelo, así
    que pasan por su validación normal (incluida la de stock negativo en
    Movimiento.clean())."""
    producto = forms.CharField(
        label='Producto',
        widget=forms.TextInput(attrs={
            **ATRIBUTOS_BUSCADOR_PRODUCTO, **ATRIBUTOS_VALIDACION_STOCK, 'hx-trigger': 'change, blur',
        }),
    )

    class Meta:
        model = Movimiento
        fields = ['producto', 'tipo', 'cantidad', 'documento', 'motivo']
        labels = {'documento': 'Documento (opcional)', 'motivo': 'Motivo (opcional)'}
        widgets = {
            'tipo': forms.Select(attrs={'class': 'form-select', **ATRIBUTOS_VALIDACION_STOCK, 'hx-trigger': 'change'}),
            'cantidad': forms.NumberInput(attrs={
                'class': 'form-control', 'min': 1,
                **ATRIBUTOS_VALIDACION_STOCK, 'hx-trigger': 'input changed delay:400ms, change',
            }),
            'documento': forms.TextInput(attrs={'class': 'form-control'}),
            'motivo': forms.TextInput(attrs={'class': 'form-control'}),
        }

    def clean_producto(self):
        texto = self.cleaned_data['producto']
        producto = resolver_producto(texto)
        if producto is None:
            raise forms.ValidationError('Selecciona un producto de la lista (busca por código o nombre).')
        return producto


def _estilo_bootstrap(form):
    """Pone la clase de Bootstrap que corresponde a cada campo, para no
    repetir widgets en cada ModelForm."""
    for campo in form.fields.values():
        widget = campo.widget
        if isinstance(widget, forms.CheckboxInput):
            clase = 'form-check-input'
        elif isinstance(widget, forms.Select):
            clase = 'form-select'
        else:
            clase = 'form-control'
        widget.attrs.setdefault('class', clase)


class ProductoForm(forms.ModelForm):
    """Alta y edición de un producto del catálogo. No incluye el stock: el
    stock solo cambia con movimientos (ingresos, salidas, ajustes) y con
    la recepción de compras, para que siempre quede el rastro. El código
    no se puede cambiar después de creado: es la llave con la que las
    fichas de registro y los pedidos identifican al producto."""

    class Meta:
        model = Producto
        fields = [
            'codigo', 'nombre', 'presentacion', 'color', 'marca', 'categoria',
            'precio_venta', 'costo_compra', 'stock_minimo', 'lead_time_dias', 'activo',
        ]
        labels = {
            'codigo': 'Código',
            'presentacion': 'Presentación',
            'categoria': 'Categoría',
            'precio_venta': 'Precio de venta (S/)',
            'costo_compra': 'Costo de compra (S/)',
            'stock_minimo': 'Stock mínimo',
            'lead_time_dias': 'Lead time (días)',
        }
        help_texts = {
            'categoria': 'El pronóstico de demanda se calcula por categoría: cambiarla mueve las ventas '
                         'de este producto a otra categoría y exige volver a entrenar el modelo.',
            'precio_venta': 'No cambia los pedidos ya registrados: cada línea conserva su precio.',
            'lead_time_dias': 'Días que tarda el proveedor en entregar. Se usa en el stock de seguridad.',
            'activo': 'Un producto inactivo no aparece en los buscadores ni recibe recomendaciones.',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _estilo_bootstrap(self)
        if self.instance.pk:
            self.fields['codigo'].disabled = True
        for nombre in ('precio_venta', 'costo_compra'):
            self.fields[nombre].min_value = 0
            self.fields[nombre].widget.attrs['min'] = '0'
        self.fields['stock_minimo'].widget.attrs['min'] = '0'

    def clean_codigo(self):
        return self.cleaned_data['codigo'].strip().upper()

    def clean(self):
        cleaned = super().clean()
        for nombre in ('precio_venta', 'costo_compra', 'stock_minimo'):
            valor = cleaned.get(nombre)
            if valor is not None and valor < 0:
                self.add_error(nombre, 'No puede ser negativo.')
        precio, costo = cleaned.get('precio_venta'), cleaned.get('costo_compra')
        if precio is not None and costo is not None and precio < costo:
            self.add_error('precio_venta', 'El precio de venta es menor que el costo de compra.')
        return cleaned


class ProveedorForm(forms.ModelForm):
    class Meta:
        model = Proveedor
        fields = ['nombre', 'contacto', 'telefono', 'lead_time_promedio']
        labels = {
            'contacto': 'Persona de contacto',
            'telefono': 'Teléfono',
            'lead_time_promedio': 'Lead time promedio (días)',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _estilo_bootstrap(self)
        self.fields['lead_time_promedio'].widget.attrs['min'] = '0'

    def clean_lead_time_promedio(self):
        valor = self.cleaned_data['lead_time_promedio']
        if valor < 0:
            raise forms.ValidationError('No puede ser negativo.')
        return valor


class CompraForm(forms.ModelForm):
    """Cabecera de una compra (las líneas van en LineaCompraFormSet)."""

    class Meta:
        model = Compra
        fields = ['proveedor', 'fecha_pedido']
        labels = {'fecha_pedido': 'Fecha del pedido al proveedor'}
        widgets = {'fecha_pedido': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d')}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _estilo_bootstrap(self)
        self.fields['proveedor'].empty_label = 'Elige un proveedor'
        if not self.is_bound and not self.initial.get('fecha_pedido'):
            self.initial['fecha_pedido'] = date.today()


class LineaCompraForm(forms.Form):
    """Una línea de la compra: producto, cantidad y costo unitario.
    recomendacion es el id de la recomendación de la que salió la línea,
    cuando la compra se arma desde las recomendaciones aceptadas."""
    producto = forms.CharField(label='Producto', widget=forms.TextInput(attrs=ATRIBUTOS_BUSCADOR_PRODUCTO))
    cantidad_pedida = forms.IntegerField(
        label='Cantidad', min_value=1, widget=forms.NumberInput(attrs={'class': 'form-control', 'min': 1}),
    )
    costo_unitario = forms.DecimalField(
        label='Costo unitario (S/)', min_value=0, max_digits=12, decimal_places=2,
        widget=forms.NumberInput(attrs={'class': 'form-control', 'min': 0, 'step': '0.01'}),
    )
    recomendacion = forms.IntegerField(required=False, widget=forms.HiddenInput())

    def clean_producto(self):
        producto = resolver_producto(self.cleaned_data['producto'])
        if producto is None:
            raise forms.ValidationError('Selecciona un producto de la lista (busca por código o nombre).')
        return producto


class _LineasCompraFormSet(forms.BaseFormSet):
    def clean(self):
        """Un mismo producto no puede ir en dos líneas de la misma compra:
        se sumaría a mano y la recepción quedaría partida."""
        if any(self.errors):
            return
        vistos = set()
        for form in self.forms:
            datos = form.cleaned_data
            if not datos or datos.get('DELETE'):
                continue
            if datos['producto'].pk in vistos:
                raise forms.ValidationError(
                    f'El producto {datos["producto"].codigo} está en más de una línea: déjalo en una sola.'
                )
            vistos.add(datos['producto'].pk)


LineaCompraFormSet = forms.formset_factory(
    LineaCompraForm, formset=_LineasCompraFormSet, extra=1, can_delete=True,
)
