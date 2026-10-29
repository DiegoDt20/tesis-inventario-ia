"""Genera la tabla de correcciones entre dos versiones de una ficha de
registro (ver inventario/servicios/correcciones_ficha.py)."""
import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from inventario.servicios.correcciones_ficha import _texto, comparar_fichas

# Motivo de las diferencias que no son correcciones de registro ni de
# periodo, por hoja y campo. Lo que no está aquí queda "sin motivo
# declarado": no se inventa uno.
MOTIVOS_OTRAS = {
    ('2_NS', '¿A tiempo? (1/0)'): 'Recalculado por la ficha a partir de la cantidad y la fecha de atención corregidas.',
    ('2_NS', 'Motivo'): 'Redacción del motivo de atención; el sistema no usa esta columna.',
    ('3_COI_PD', '(fila completa)'): 'Derivado de 2_NS: el pedido quedó atendido por completo.',
    ('3_COI_PD', 'Código del producto'): 'Derivado de 2_NS (reasignación de códigos).',
}
ENCABEZADO = ('Hoja', 'N.° de registro', 'Campo corregido', 'Valor anterior', 'Valor corregido', 'Motivo')


class Command(BaseCommand):
    help = 'Tabla de correcciones entre la ficha anterior y la corregida, para el anexo.'

    def add_arguments(self, parser):
        parser.add_argument('--anterior', required=True, help='Ficha anterior (.xlsx).')
        parser.add_argument('--corregida', required=True, help='Ficha corregida (.xlsx).')
        parser.add_argument(
            '--motivo', action='append', default=[], metavar='HOJA:N:MOTIVO',
            help='Motivo de una diferencia de "otras" (hoja, N.° de registro), p. ej. '
                 '"1_EI:261:Código duplicado". Se puede repetir.',
        )
        parser.add_argument('--salida', required=True, metavar='RUTA_SIN_EXTENSION',
                            help='Guarda RUTA.csv y RUTA.md.')

    def handle(self, *args, **options):
        for ruta in (options['anterior'], options['corregida']):
            if not Path(ruta).exists():
                raise CommandError(f'No se encontró el archivo: {ruta}')
        motivos_registro = {}
        for texto in options['motivo']:
            hoja, _, resto = texto.partition(':')
            numero, _, motivo = resto.partition(':')
            motivos_registro[(hoja, numero)] = motivo

        registro, periodo, otras = comparar_fichas(options['anterior'], options['corregida'])
        for c in otras:
            c.motivo = (
                motivos_registro.get((c.hoja, str(c.registro)))
                or MOTIVOS_OTRAS.get((c.hoja, c.campo))
                or 'Sin motivo declarado por la empresa.'
            )

        base = Path(options['salida'])
        base.parent.mkdir(parents=True, exist_ok=True)
        secciones = [
            ('A', 'Correcciones de registro', registro),
            ('B', 'Ventana de medición', periodo),
            ('C', 'Otras diferencias entre los archivos', otras),
        ]
        fila = lambda c: (c.hoja, str(c.registro), c.campo, _texto(c.anterior), _texto(c.corregido), c.motivo)
        with open(base.with_suffix('.csv'), 'w', newline='', encoding='utf-8') as archivo:
            escritor = csv.writer(archivo)
            escritor.writerow(('Sección', *ENCABEZADO))
            for letra, _titulo, filas in secciones:
                escritor.writerows((letra, *fila(c)) for c in filas)
        with open(base.with_suffix('.md'), 'w', encoding='utf-8') as archivo:
            archivo.write('# Correcciones de la ficha del pretest\n\n')
            archivo.write(
                f'Generado por `comparar_fichas` comparando `{Path(options["anterior"]).name}` '
                f'(anterior) con `{Path(options["corregida"]).name}` (corregida), celda por celda.\n'
            )
            for letra, titulo, filas in secciones:
                archivo.write(f'\n## {letra}. {titulo} ({len(filas)})\n\n')
                if letra == 'C':
                    archivo.write(
                        'Las diferencias iguales (mismo campo, valores y motivo) se agrupan en '
                        'una fila con todos sus N.°; el CSV trae una fila por registro.\n\n'
                    )
                archivo.write('| ' + ' | '.join(ENCABEZADO) + ' |\n|' + '---|' * len(ENCABEZADO) + '\n')
                for valores in (self._agrupar(filas, fila) if letra == 'C' else map(fila, filas)):
                    archivo.write('| ' + ' | '.join(v.replace('|', '\\|') for v in valores) + ' |\n')

        self._resumir(registro, periodo, otras, base)

    @staticmethod
    def _agrupar(correcciones, fila):
        """Une las filas que solo difieren en el N.° de registro."""
        grupos = {}
        for c in correcciones:
            hoja, registro, *resto = fila(c)
            grupos.setdefault((hoja, *resto), []).append(registro)
        return [
            (hoja, ', '.join(registros), *resto)
            for (hoja, *resto), registros in grupos.items()
        ]

    def _resumir(self, registro, periodo, otras, base):
        self.stdout.write(self.style.SUCCESS(
            f'Correcciones de registro: {len(registro)} ({len({c.registro for c in registro})} registro(s)) · '
            f'periodo: {len(periodo)} · otras diferencias: {len(otras)}'
        ))
        self.stdout.write(f'Tabla guardada en {base.with_suffix(".csv")} y {base.with_suffix(".md")}')
