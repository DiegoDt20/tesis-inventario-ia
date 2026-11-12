"""Compara EI, NS y COI entre dos meses (el pretest y el postest) y,
opcionalmente, guarda la comparación en Excel con el detalle línea por
línea. No escribe en la base de datos."""
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError

from inventario.models import Origen
from inventario.servicios.comparacion_periodos import (
    comparar_periodos,
    libro_comparacion,
    meses_con_datos,
    rango_mes,
)


def _mes(texto):
    try:
        return datetime.strptime(texto, '%Y-%m').date()
    except ValueError:
        raise CommandError(f'Mes inválido "{texto}": usa el formato AAAA-MM.')


def _valor(fila, campo):
    valor = fila[campo]
    if valor is None:
        return '—'
    return f'{valor:.2f} %' if fila['unidad'] == '%' else f'S/ {valor:,.2f}'


class Command(BaseCommand):
    help = (
        'Compara EI, NS y COI entre dos meses. Sin --antes/--despues usa los dos '
        'últimos meses con datos del origen.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--antes', help='Mes del pretest, AAAA-MM.')
        parser.add_argument('--despues', help='Mes del postest, AAAA-MM.')
        parser.add_argument('--origen', choices=[Origen.PRUEBA, Origen.REAL], default=Origen.REAL)
        parser.add_argument('--excel', help='Ruta .xlsx donde guardar la comparación con su detalle.')

    def handle(self, *args, **opciones):
        origen = opciones['origen']
        meses = meses_con_datos(origen)
        despues = _mes(opciones['despues']) if opciones['despues'] else (meses[-1] if meses else None)
        if despues is None:
            raise CommandError(f'No hay datos del origen "{origen}" para comparar.')
        if opciones['antes']:
            antes = _mes(opciones['antes'])
        else:
            previos = [m for m in meses if m < despues]
            if not previos:
                raise CommandError(
                    f'No hay un mes con datos anterior a {despues:%Y-%m} (origen "{origen}"): indica --antes.'
                )
            antes = previos[-1]

        resultado = comparar_periodos(rango_mes(antes), rango_mes(despues), origen=origen)

        self.stdout.write(f'Comparación de indicadores (origen "{origen}"): {antes:%Y-%m} → {despues:%Y-%m}')
        self.stdout.write(f"{'Indicador':<40}{'Antes':>16}{'Después':>16}{'Diferencia':>16}{'Variación':>12}")
        for fila in resultado['filas']:
            nombre = ('  ' if fila['secundaria'] else '') + fila['nombre']
            if fila['diferencia'] is None:
                diferencia = '—'
            elif fila['unidad'] == '%':
                diferencia = f"{fila['diferencia']:+.2f} pp"
            else:
                diferencia = f"{fila['diferencia']:+,.2f}"
            variacion = f"{fila['variacion']:+.1f} %" if fila['variacion'] is not None else '—'
            self.stdout.write(
                f"{nombre:<40}{_valor(fila, 'antes'):>16}{_valor(fila, 'despues'):>16}{diferencia:>16}{variacion:>12}"
            )

        if opciones['excel']:
            libro_comparacion(resultado).save(opciones['excel'])
            self.stdout.write(self.style.SUCCESS(f"Excel guardado en {opciones['excel']}"))
