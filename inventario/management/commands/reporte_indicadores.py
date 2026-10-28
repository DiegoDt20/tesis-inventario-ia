"""Reporta EI, NS y COI de un periodo, con el COI desglosado (CA + PD) para
cuadrarlo contra la ficha, y guarda o compara una foto de los valores para
comprobar que una carga posterior (el postest) no cambió un periodo ya
medido (el pretest)."""
import json
from datetime import datetime
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from inventario.models import Origen
from inventario.servicios.indicadores import (
    calcular_coi,
    calcular_ei,
    calcular_ns,
    lineas_desabastecimiento,
)


def _fecha(texto):
    try:
        return datetime.strptime(texto, '%Y-%m-%d').date()
    except ValueError:
        raise CommandError(f'Fecha inválida "{texto}": usa el formato AAAA-MM-DD.')


def foto_indicadores(desde, hasta, origen):
    """Valores exactos de los tres indicadores del periodo, serializables a
    JSON sin perder precisión: los conteos de EI/NS como enteros (el
    porcentaje se deriva de ellos) y los montos del COI como texto
    (Decimal), para que la comparación sea exacta, sin redondeo."""
    ei = calcular_ei(desde, hasta, origen=origen)
    ns = calcular_ns(desde, hasta, origen=origen)
    coi = calcular_coi(desde, hasta, origen=origen)
    return {
        'origen': origen,
        'desde': desde.isoformat(),
        'hasta': hasta.isoformat(),
        'ei': {'correctos': ei['correctos'], 'total': ei['total'], 'valor': ei['valor']},
        'ns': {'a_tiempo': ns['a_tiempo'], 'total': ns['total'], 'valor': ns['valor']},
        'coi': {
            'valor': str(coi['valor']),
            'almacenamiento': str(coi['almacenamiento']),
            'desabastecimiento': str(coi['desabastecimiento']),
            'conceptos_almacenamiento': len(coi['conceptos_almacenamiento']),
            'lineas_desabastecimiento': coi['lineas_desabastecimiento'],
            'unidades_no_atendidas': coi['unidades_no_atendidas'],
            'productos_afectados': coi['productos_afectados'],
        },
    }


def _diferencias(antes, ahora, ruta=''):
    """Lista de (clave, antes, ahora) de todo lo que no es idéntico."""
    if isinstance(antes, dict) and isinstance(ahora, dict):
        salida = []
        for clave in sorted(set(antes) | set(ahora)):
            salida.extend(_diferencias(antes.get(clave), ahora.get(clave), f'{ruta}.{clave}'.lstrip('.')))
        return salida
    return [] if antes == ahora else [(ruta, antes, ahora)]


class Command(BaseCommand):
    help = (
        'Reporta EI, NS y COI de un periodo con el COI desglosado (CA + PD). '
        'Con --detalle lista cada línea del PD para cuadrarlo contra la hoja '
        '3_COI_PD. Con --guardar escribe una foto exacta de los valores en JSON; '
        'con --comparar la compara con una foto anterior y falla si algún valor '
        'cambió (p. ej. el pretest después de importar el postest). No escribe '
        'nada en la base de datos.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--desde', required=True, type=_fecha, help='Inicio del periodo (AAAA-MM-DD).')
        parser.add_argument('--hasta', required=True, type=_fecha, help='Fin del periodo, inclusive (AAAA-MM-DD).')
        parser.add_argument(
            '--origen', default=Origen.REAL, choices=Origen.values,
            help='Origen de los datos (por defecto: real).',
        )
        parser.add_argument('--detalle', action='store_true', help='Lista cada línea del PD.')
        parser.add_argument('--guardar', help='Escribe la foto de los valores en este archivo JSON.')
        parser.add_argument(
            '--comparar',
            help='Compara con la foto guardada en este archivo JSON y falla si algo cambió.',
        )

    def handle(self, *args, **options):
        desde, hasta, origen = options['desde'], options['hasta'], options['origen']
        if desde > hasta:
            raise CommandError('--desde no puede ser posterior a --hasta.')

        foto = foto_indicadores(desde, hasta, origen)
        coi = calcular_coi(desde, hasta, origen=origen)
        self._imprimir(foto, coi)
        if options['detalle']:
            self._imprimir_detalle(desde, hasta, origen)

        if options['guardar']:
            ruta = Path(options['guardar'])
            ruta.parent.mkdir(parents=True, exist_ok=True)
            ruta.write_text(json.dumps(foto, indent=2, ensure_ascii=False))
            self.stdout.write(self.style.SUCCESS(f'\nFoto guardada en {ruta}'))

        if options['comparar']:
            self._comparar(Path(options['comparar']), foto)

    def _imprimir(self, foto, coi):
        ei, ns = foto['ei'], foto['ns']
        self.stdout.write(self.style.SUCCESS(
            f'Indicadores de origen "{foto["origen"]}" del {foto["desde"]} al {foto["hasta"]}'
        ))
        ei_texto = f'{ei["valor"]:.2f}%' if ei['valor'] is not None else 'sin datos'
        ns_texto = f'{ns["valor"]:.2f}%' if ns['valor'] is not None else 'sin datos'
        self.stdout.write(f'  EI  = {ei_texto} ({ei["correctos"]}/{ei["total"]} registros correctos)')
        self.stdout.write(f'  NS  = {ns_texto} ({ns["a_tiempo"]}/{ns["total"]} pedidos a tiempo)')
        self.stdout.write(f'  COI = CA + PD = S/ {coi["valor"]:.2f}')
        self.stdout.write(
            f'  CA  = suma de costos de almacenamiento del periodo   → S/ {coi["almacenamiento"]:.2f}'
        )
        for concepto in coi['conceptos_almacenamiento']:
            self.stdout.write(
                f'        {concepto["periodo_mes"]:%m/%Y}  {concepto["concepto"][:70]:<70}  '
                f'S/ {concepto["monto"]:>10.2f}'
            )
        self.stdout.write(
            f'  PD  = Σ (solicitada − atendida) × margen unitario  → S/ {coi["desabastecimiento"]:.2f}'
        )
        self.stdout.write(
            f'        {coi["lineas_desabastecimiento"]} línea(s), {coi["unidades_no_atendidas"]} '
            f'unidad(es) no atendida(s), {coi["productos_afectados"]} producto(s); margen = '
            'precio de venta − costo de compra congelados en cada línea'
        )
        if coi['lineas_precio_reconstruido']:
            self.stdout.write(self.style.WARNING(
                f'        {coi["lineas_precio_reconstruido"]} línea(s) con precio reconstruido '
                '(PedidoDetalle.precios_reconstruidos), no el registrado en su momento.'
            ))

    def _imprimir_detalle(self, desde, hasta, origen):
        lineas = (
            lineas_desabastecimiento(desde, hasta, origen)
            .select_related('pedido', 'producto')
            .order_by('pedido__fecha_solicitud', 'pk')
        )
        self.stdout.write('\nLíneas del PD:')
        self.stdout.write(
            f'  {"Fecha":<10}  {"Cliente":<8}  {"Código":<9}  {"No atend.":>9}  '
            f'{"Precio":>8}  {"Costo":>8}  {"Margen":>7}  {"Pérdida":>9}  Reconstr.'
        )
        for linea in lineas:
            no_atendida = linea.cantidad_solicitada - linea.cantidad_atendida
            margen = linea.precio_venta_unitario - linea.costo_compra_unitario
            self.stdout.write(
                f'  {timezone.localdate(linea.pedido.fecha_solicitud):%d/%m/%Y}  '
                f'{linea.pedido.cliente[:8]:<8}  {linea.producto.codigo:<9}  {no_atendida:>9}  '
                f'{linea.precio_venta_unitario:>8.2f}  {linea.costo_compra_unitario:>8.2f}  '
                f'{margen:>7.2f}  {no_atendida * margen:>9.2f}  '
                f'{"sí" if linea.precios_reconstruidos else "no"}'
            )

    def _comparar(self, ruta, foto):
        if not ruta.exists():
            raise CommandError(f'No se encontró la foto a comparar: {ruta}')
        anterior = json.loads(ruta.read_text())
        diferencias = _diferencias(anterior, foto)
        if diferencias:
            detalle = '\n'.join(f'  {clave}: {antes!r} → {ahora!r}' for clave, antes, ahora in diferencias)
            raise CommandError(f'Los indicadores cambiaron respecto de {ruta}:\n{detalle}')
        self.stdout.write(self.style.SUCCESS(f'\nSin cambios respecto de {ruta}: los valores son idénticos.'))
