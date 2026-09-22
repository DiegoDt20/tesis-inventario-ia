"""Formularios de inventario.

Las pantallas de operación diaria (pedidos, movimientos, conteos) están
pensadas para el dueño de la microempresa, sin conocimientos técnicos: los
formularios validan de más antes que de menos, y los mensajes de error se
redactan en lenguaje sencillo.
"""
from datetime import date

from django import forms
from django.contrib.auth.forms import AuthenticationForm

from .models import Movimiento, Pedido, PedidoDetalle, Producto


class LoginForm(AuthenticationForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for campo in self.fields.values():
            campo.widget.attrs.setdefault('class', 'form-control')


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
