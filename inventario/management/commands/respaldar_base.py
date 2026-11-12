"""Respalda la base de datos con pg_dump (formato propio de PostgreSQL,
restaurable con pg_restore) y rota los respaldos automáticos antiguos.

El pretest y el postest ya cargados solo viven en la base y en las fichas
de Excel: este comando es la copia de seguridad de la base. Solo lee."""
import os
import subprocess
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.text import slugify

DIR_RESPALDOS = Path(settings.BASE_DIR) / 'artefactos' / 'respaldos'
# Solo los respaldos con este prefijo entran en la rotación: los que llevan
# etiqueta (respaldo_<etiqueta>_...) se hicieron a propósito antes de un
# cambio y nunca se borran solos.
PREFIJO_AUTOMATICO = 'auto_'


def _entorno_y_conexion():
    """Argumentos de conexión y entorno para pg_dump/pg_restore. La
    contraseña va por variable de entorno, nunca en la línea de comandos
    (que cualquier usuario del equipo puede ver)."""
    bd = settings.DATABASES['default']
    argumentos = ['--dbname', bd['NAME']]
    if bd.get('USER'):
        argumentos += ['--username', bd['USER']]
    if bd.get('HOST'):
        argumentos += ['--host', bd['HOST']]
    if bd.get('PORT'):
        argumentos += ['--port', str(bd['PORT'])]
    entorno = dict(os.environ)
    if bd.get('PASSWORD'):
        entorno['PGPASSWORD'] = bd['PASSWORD']
    return argumentos, entorno


class Command(BaseCommand):
    help = (
        'Respalda la base de datos en artefactos/respaldos/ con pg_dump y conserva solo los '
        'últimos respaldos automáticos. Con --etiqueta el respaldo no se borra nunca solo.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--destino', default=None, help='Carpeta de respaldos (artefactos/respaldos).')
        parser.add_argument(
            '--conservar', type=int, default=14,
            help='Cuántos respaldos automáticos conservar (por defecto 14). No afecta a los etiquetados.',
        )
        parser.add_argument(
            '--etiqueta', default=None,
            help='Nombre del respaldo hecho a propósito (p. ej. "antes_postest"); queda fuera de la rotación.',
        )

    def handle(self, *args, **opciones):
        if opciones['conservar'] < 1:
            raise CommandError('--conservar debe ser 1 o más.')
        destino = Path(opciones['destino']) if opciones['destino'] else DIR_RESPALDOS
        destino.mkdir(parents=True, exist_ok=True)

        marca = timezone.localtime().strftime('%Y%m%d_%H%M%S')
        if opciones['etiqueta']:
            etiqueta = slugify(opciones['etiqueta']).replace('-', '_')
            if not etiqueta:
                raise CommandError('--etiqueta no tiene ningún carácter utilizable.')
            nombre = f'respaldo_{etiqueta}_{marca}.dump'
        else:
            nombre = f'{PREFIJO_AUTOMATICO}{marca}.dump'
        ruta = destino / nombre
        # Se escribe con otro nombre y se renombra al final: un respaldo a
        # medias nunca queda con nombre de respaldo válido.
        parcial = destino / (nombre + '.parcial')

        conexion, entorno = _entorno_y_conexion()
        try:
            self._ejecutar(['pg_dump', '--format=custom', '--file', str(parcial)] + conexion, entorno)
            # Comprobar que el archivo se puede leer: lista su contenido.
            self._ejecutar(['pg_restore', '--list', str(parcial)], entorno)
        except CommandError:
            parcial.unlink(missing_ok=True)
            raise
        parcial.rename(ruta)
        os.chmod(ruta, 0o600)

        borrados = []
        if not opciones['etiqueta']:
            automaticos = sorted(destino.glob(f'{PREFIJO_AUTOMATICO}*.dump'))
            for viejo in automaticos[:-opciones['conservar']]:
                viejo.unlink()
                borrados.append(viejo.name)

        self.stdout.write(self.style.SUCCESS(
            f'Respaldo guardado: {ruta} ({ruta.stat().st_size / 1024:.0f} KB)'
        ))
        if borrados:
            self.stdout.write(f'Respaldos automáticos antiguos borrados: {len(borrados)}')
        self.stdout.write(
            'Para restaurar (reemplaza el contenido de la base): '
            f'pg_restore --clean --if-exists --no-owner --dbname {settings.DATABASES["default"]["NAME"]} "{ruta}"'
        )

    def _ejecutar(self, comando, entorno):
        try:
            resultado = subprocess.run(comando, env=entorno, capture_output=True, text=True)
        except FileNotFoundError:
            raise CommandError(f'No se encontró "{comando[0]}": instala las herramientas cliente de PostgreSQL.')
        if resultado.returncode != 0:
            raise CommandError(f'{comando[0]} falló: {resultado.stderr.strip() or "sin mensaje"}')
        return resultado
