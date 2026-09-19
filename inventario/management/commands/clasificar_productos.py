"""Comando de gestión: clasifica los productos por categoría según palabras
clave en el nombre, para poder entrenar y predecir la demanda agrupando por
categoría (ver inventario/ml/carga_interna.py)."""
import unicodedata

from django.core.management.base import BaseCommand, CommandError

from inventario.models import Categoria, Producto

# El orden importa: accesorio y solvente se evalúan antes que el resto,
# porque un nombre como "base de rodillo" es un accesorio, no una base, y
# "aguarrás mineral" no debe caer en ninguna otra categoría por accidente.
REGLAS_CLASIFICACION = [
    (Categoria.ACCESORIO, ['brocha', 'rodillo', 'espatula', 'lija', 'cinta', 'bandeja']),
    (Categoria.SOLVENTE, ['thinner', 'aguarras', 'disolvente']),
    (Categoria.TEMPLE, ['temple']),
    (Categoria.LATEX, ['latex', 'cpp pato', 'innova']),
    (Categoria.ESMALTE, ['gloss', 'esmalte', 'satinado', 'mate', 'brillante']),
    (Categoria.BASE, ['base', 'imprimante', 'sellador']),
]


def _normalizar(texto):
    """Minúsculas y sin tildes, para que "látex" y "aguarrás" coincidan con
    las palabras clave sin tener que repetirlas con y sin acento."""
    sin_tildes = unicodedata.normalize('NFKD', texto).encode('ascii', 'ignore').decode('ascii')
    return sin_tildes.lower()


def clasificar_producto(producto):
    """Devuelve la Categoria que corresponde a `producto` según
    REGLAS_CLASIFICACION, evaluando las reglas en orden y quedándose con la
    primera que coincida. Busca las palabras clave en nombre, color y marca:
    en los datos reales el tipo de producto (thinner, sellador, etc.) suele
    venir en el campo "color" y no en "nombre" (que a veces solo trae la
    marca, p. ej. nombre="Anypsa", color="Thinner extra acrílico"). Si
    ninguna regla coincide, devuelve Categoria.OTRO."""
    texto = _normalizar(' '.join(filter(None, [producto.nombre, producto.color, producto.marca])))
    for categoria, palabras_clave in REGLAS_CLASIFICACION:
        if any(_normalizar(palabra) in texto for palabra in palabras_clave):
            return categoria
    return Categoria.OTRO


class Command(BaseCommand):
    help = (
        'Clasifica los productos por categoría según palabras clave en el '
        'nombre. Sin --codigo/--categoria, reclasifica TODOS los productos; '
        'con ambos, corrige un único producto puntual.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--codigo', default=None,
            help='Código de un producto puntual a corregir (usar junto con --categoria).',
        )
        parser.add_argument(
            '--categoria', default=None, choices=Categoria.values,
            help='Categoría a asignar al producto de --codigo.',
        )

    def handle(self, *args, **options):
        codigo = options['codigo']
        categoria = options['categoria']

        if codigo or categoria:
            if not (codigo and categoria):
                raise CommandError('--codigo y --categoria deben usarse juntos.')
            self._corregir_uno(codigo, categoria)
            return

        self._clasificar_todos()

    def _corregir_uno(self, codigo, categoria):
        try:
            producto = Producto.objects.get(codigo=codigo)
        except Producto.DoesNotExist as exc:
            raise CommandError(f'No existe ningún producto con código "{codigo}".') from exc

        producto.categoria = categoria
        producto.save(update_fields=['categoria'])
        self.stdout.write(self.style.SUCCESS(
            f'{producto.codigo} - {producto.nombre}: categoría asignada a "{categoria}".'
        ))

    def _clasificar_todos(self):
        productos = list(Producto.objects.all())
        actualizados = 0
        for producto in productos:
            nueva_categoria = clasificar_producto(producto)
            if nueva_categoria != producto.categoria:
                producto.categoria = nueva_categoria
                producto.save(update_fields=['categoria'])
                actualizados += 1

        self.stdout.write(self.style.SUCCESS(
            f'Productos clasificados: {len(productos)} ({actualizados} cambiaron de categoría).'
        ))

        self.stdout.write('\nConteo por categoría:')
        conteos = {categoria: 0 for categoria in Categoria.values}
        for producto in Producto.objects.all():
            conteos[producto.categoria] += 1
        for categoria in Categoria.values:
            self.stdout.write(f'  {categoria:<12}{conteos[categoria]}')

        sin_clasificar = Producto.objects.filter(categoria=Categoria.OTRO)
        if sin_clasificar.exists():
            self.stdout.write(f'\nProductos que quedaron en "otro" ({sin_clasificar.count()}):')
            for producto in sin_clasificar:
                self.stdout.write(f'  {producto.codigo} - {producto.nombre}')
        else:
            self.stdout.write('\nNingún producto quedó sin clasificar.')
