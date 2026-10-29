"""Reconstruye el catálogo de productos desde la ficha de registro (hoja
1_EI como fuente oficial). Ver inventario/servicios/catalogo_ficha.py."""
import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from inventario.models import Categoria
from inventario.servicios import catalogo_ficha

NOMBRES_CAMPO = {
    'nombre': 'Nombre', 'color': 'Color', 'presentacion': 'Presentación',
    'precio_venta': 'Precio de venta (S/)', 'costo_compra': 'Costo de compra (S/)',
    'categoria': 'Categoría', 'activo': 'Activo',
}


def _texto(valor):
    if valor is True:
        return 'Sí'
    if valor is False:
        return 'No'
    return '' if valor is None else str(valor)


class Command(BaseCommand):
    help = (
        'Reconstruye el catálogo desde la ficha: nombre, color y presentación '
        'de 1_EI, precio y costo de 3_COI_PD, categoría conservada por código, '
        'y los productos que no están en la ficha quedan inactivos (no se borran).'
    )

    def add_arguments(self, parser):
        parser.add_argument('--archivo', required=True, help='Ficha de registro (.xlsx).')
        parser.add_argument(
            '--categoria', action='append', default=[], metavar='CODIGO=CATEGORIA',
            help='Fija la categoría de un producto (se puede repetir).',
        )
        parser.add_argument(
            '--tabla', metavar='RUTA_SIN_EXTENSION',
            help='Guarda la tabla de cambios en RUTA.csv y RUTA.md.',
        )
        parser.add_argument('--dry-run', action='store_true', help='Solo reporta; no escribe.')

    def handle(self, *args, **options):
        ruta = Path(options['archivo'])
        if not ruta.exists():
            raise CommandError(f'No se encontró el archivo: {ruta}')
        forzadas = {}
        for texto in options['categoria']:
            codigo, _, categoria = texto.partition('=')
            if categoria not in Categoria.values:
                raise CommandError(f'--categoria "{texto}": categoría no válida ({", ".join(Categoria.values)}).')
            forzadas[codigo.strip()] = categoria

        plan = catalogo_ficha.planificar(ruta, categorias_forzadas=forzadas)
        if plan.faltan_en_catalogo or plan.sin_separar:
            for codigo, texto in plan.faltan_en_catalogo:
                self.stdout.write(f'  No existe en el catálogo: {codigo} "{texto}"')
            for codigo, texto in plan.sin_separar:
                self.stdout.write(f'  No se pudo separar en nombre/color/presentación: {codigo} "{texto}"')
            raise CommandError('La ficha trae productos que no se pueden reconstruir. No se escribió nada.')

        with transaction.atomic():
            productos = catalogo_ficha.aplicar(plan)
            self._reportar(plan, productos)
            if options['tabla']:
                self._guardar_tabla(plan, Path(options['tabla']), ruta)
            if options['dry_run']:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING('\nDry-run: no se escribió ningún cambio.'))

    def _reportar(self, plan, productos):
        por_campo = {}
        for _codigo, campo, _a, _n in plan.cambios:
            por_campo[campo] = por_campo.get(campo, 0) + 1
        self.stdout.write(self.style.SUCCESS(
            f'Catálogo reconstruido: {len(plan.cambios)} cambio(s) en {productos} producto(s).'
        ))
        for campo, n in sorted(por_campo.items()):
            self.stdout.write(f'  {NOMBRES_CAMPO[campo]}: {n}')

        self.stdout.write(f'\nInactivados (no están en 1_EI): {len(plan.inactivados)}')
        for codigo, texto in plan.inactivados:
            self.stdout.write(f'  {codigo}  {texto}')

        self.stdout.write(f'\nCategorías deducidas del nombre (estaban en "otro"): {len(plan.categorias_deducidas)}')
        for codigo, texto, categoria in plan.categorias_deducidas:
            self.stdout.write(f'  {codigo}  {texto} -> {categoria}')

        self.stdout.write(
            f'\nCategoría conservada distinta de la que sugiere el nombre nuevo '
            f'(no se cambió): {len(plan.categorias_en_conflicto)}'
        )
        for codigo, texto, conservada, deducida in plan.categorias_en_conflicto:
            self.stdout.write(f'  {codigo}  {texto}: {conservada} (el nombre sugiere {deducida})')

        codigos = sum(len(c) for c in plan.duplicados.values())
        self.stdout.write(
            f'\nDescripciones repetidas entre productos activos (vienen así en 1_EI): '
            f'{len(plan.duplicados)} grupo(s), {codigos} código(s)'
        )
        for texto, grupo in plan.duplicados.items():
            self.stdout.write(f'  {texto}: {", ".join(grupo)}')

    def _guardar_tabla(self, plan, base, ficha):
        base.parent.mkdir(parents=True, exist_ok=True)
        filas = [
            (codigo, NOMBRES_CAMPO[campo], _texto(anterior), _texto(nuevo))
            for codigo, campo, anterior, nuevo in sorted(plan.cambios, key=lambda c: (c[0], list(NOMBRES_CAMPO).index(c[1])))
        ]
        encabezado = ('Código', 'Campo', 'Valor anterior', 'Valor nuevo')
        with open(base.with_suffix('.csv'), 'w', newline='', encoding='utf-8') as archivo:
            escritor = csv.writer(archivo)
            escritor.writerow(encabezado)
            escritor.writerows(filas)
        with open(base.with_suffix('.md'), 'w', encoding='utf-8') as archivo:
            archivo.write('# Cambios en el catálogo de productos\n\n')
            archivo.write(
                f'Generado por `reconstruir_catalogo` comparando la hoja 1_EI y el bloque '
                f'3_COI_PD de `{ficha.name}` con el catálogo del sistema. '
                f'{len(filas)} cambio(s).\n\n'
            )
            archivo.write('| ' + ' | '.join(encabezado) + ' |\n|---|---|---|---|\n')
            for fila in filas:
                archivo.write('| ' + ' | '.join(v.replace('|', '\\|') for v in fila) + ' |\n')
        self.stdout.write(f'\nTabla guardada en {base.with_suffix(".csv")} y {base.with_suffix(".md")}')
